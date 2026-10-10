"""Shared assertions for the multi-persona attack-surface suite.

Each helper checks one no-leak invariant and returns a structured result
that the test wraps into a matrix entry via ``record_surface_result``.
Helpers raise ``AssertionError`` for unexpected leaks so pytest still
fails the test — the matrix is in addition to, not instead of, normal
pytest failure semantics.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable

import requests


# ---------------------------------------------------------------------------
# URL prefix handling
# ---------------------------------------------------------------------------
#
# The Flask app on staging/prod is mounted behind nginx at the ``/assets/``
# prefix (PrefixMiddleware in app.py:340). App-mounted routes need the
# prefix; blueprints registered with their own ``url_prefix`` (like the
# dashboard_builder blueprint mounted at /dashboard-builder) do NOT.
#
# Tests declare per-PATH whether the prefix should apply by passing
# ``app_mounted=True`` (default) or ``app_mounted=False``. The prefix
# itself is configurable via TEST_APP_PREFIX env var (default /assets)
# so the suite runs against any deployment that uses a different mount
# point.


def _app_prefix() -> str:
    """Return the configured URL prefix for app-mounted routes.

    Default ``/assets`` matches the nginx config in production +
    staging. Trailing slashes are stripped so callers can safely
    concat ``prefix + path``.
    """
    prefix = os.environ.get("TEST_APP_PREFIX", "/assets")
    return prefix.rstrip("/")


def _resolve_path(path: str, *, app_mounted: bool) -> str:
    """Build the final URL path, applying the app prefix where appropriate.

    ``app_mounted=True``  → prefix is prepended (Flask app-level routes).
    ``app_mounted=False`` → path is used as-is (blueprint-registered
                            routes with their own ``url_prefix``, e.g.
                            /dashboard-builder/*).
    """
    if not app_mounted:
        return path
    return f"{_app_prefix()}{path}"


# ---------------------------------------------------------------------------
# Outcome model
# ---------------------------------------------------------------------------


@dataclass
class CheckOutcome:
    """Result of a single (persona, surface) check.

    ``outcome``:
        PASS  — no forbidden identifier present
        LEAK  — at least one forbidden identifier appeared in the response
        ERROR — request failed (network, unexpected status)
        SKIP  — check did not apply to this persona (e.g. grant_user
                check against a non-grant persona)
    """

    outcome: str
    detail: str = ""


def passed(detail: str = "") -> CheckOutcome:
    return CheckOutcome("PASS", detail)


def leaked(detail: str) -> CheckOutcome:
    return CheckOutcome("LEAK", detail)


def errored(detail: str) -> CheckOutcome:
    return CheckOutcome("ERROR", detail)


def skipped(detail: str = "") -> CheckOutcome:
    return CheckOutcome("SKIP", detail)


# ---------------------------------------------------------------------------
# JSON-haystack search
# ---------------------------------------------------------------------------


def _iter_strings(obj: Any) -> Iterable[str]:
    """Yield every string leaf in a nested JSON-like structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def find_forbidden_in_payload(payload: Any, forbidden: Iterable[str]) -> list[str]:
    """Return every forbidden identifier that appears as a string leaf.

    Case-sensitive on purpose — identifiers (ESIDs, slugs, titles) are
    case-sensitive in the data model and lower-casing would mask real
    leaks (e.g. "CRU" vs a legitimate "cru-team-1").
    """
    if not forbidden:
        return []
    forbidden_set = set(forbidden)
    found: list[str] = []
    seen = set()
    for s in _iter_strings(payload):
        if s in forbidden_set and s not in seen:
            found.append(s)
            seen.add(s)
    return found


# ---------------------------------------------------------------------------
# Request helper with consistent error reporting
# ---------------------------------------------------------------------------


def get_json(
    session: requests.Session,
    base_url: str,
    path: str,
    *,
    app_mounted: bool = True,
    **params: Any,
):
    """GET an endpoint and parse JSON.

    ``app_mounted=True`` (default) prepends the configured Flask app
    prefix (TEST_APP_PREFIX, default /assets). Pass ``app_mounted=False``
    for blueprint-registered routes that mount at their own url_prefix
    (e.g. /dashboard-builder/*).

    Returns (status_code, parsed_body) on 200 with valid JSON.
    Returns (status_code, None) for non-200 or non-JSON responses —
    caller decides whether that's an error for this specific surface.
    """
    url = f"{base_url}{_resolve_path(path, app_mounted=app_mounted)}"
    try:
        r = session.get(url, params=params or None, allow_redirects=False, timeout=15)
    except requests.RequestException as e:
        return None, f"request failed: {e}"
    if r.status_code != 200:
        return r.status_code, None
    try:
        return r.status_code, r.json()
    except (ValueError, json.JSONDecodeError):
        return r.status_code, None


def get_raw(
    session: requests.Session,
    base_url: str,
    path: str,
    *,
    app_mounted: bool = True,
    **params: Any,
):
    """GET an endpoint and return (status_code, response_text).

    Used for HTML pages (User Management, Asset Explorer list views)
    where we search the rendered body for forbidden identifiers.

    Prefix-handling matches ``get_json``: ``app_mounted=True`` (default)
    prepends TEST_APP_PREFIX.
    """
    url = f"{base_url}{_resolve_path(path, app_mounted=app_mounted)}"
    try:
        r = session.get(url, params=params or None, allow_redirects=False, timeout=15)
    except requests.RequestException as e:
        return None, f"request failed: {e}"
    return r.status_code, r.text


# ---------------------------------------------------------------------------
# Generic surface checks
# ---------------------------------------------------------------------------


def check_no_forbidden_in_json(
    session: requests.Session,
    base_url: str,
    path: str,
    forbidden: Iterable[str],
    *,
    params: dict[str, Any] | None = None,
    expect_status: int = 200,
    app_mounted: bool = True,
) -> CheckOutcome:
    """Generic check: GET path, parse JSON, scan for forbidden identifiers.

    Returns LEAK if any forbidden string appears as a leaf in the
    response body; PASS otherwise. ERROR for unexpected status or
    non-JSON response.

    ``app_mounted`` controls URL prefix handling — see ``_resolve_path``.
    """
    params = params or {}
    status, body = get_json(session, base_url, path, app_mounted=app_mounted, **params)
    if status is None:
        return errored(str(body))
    if status != expect_status:
        return errored(f"unexpected HTTP {status} from {path}")
    if body is None:
        return errored(f"non-JSON response from {path}")
    found = find_forbidden_in_payload(body, forbidden)
    if found:
        return leaked(f"forbidden identifiers in {path}: {found}")
    return passed(f"{path} clean")


def check_no_forbidden_in_html(
    session: requests.Session,
    base_url: str,
    path: str,
    forbidden: Iterable[str],
    *,
    expect_status: int = 200,
    app_mounted: bool = True,
) -> CheckOutcome:
    """Generic check: GET path, scan HTML body for forbidden identifier substrings.

    Less precise than the JSON check (substring match in raw HTML rather
    than structured leaf scan) but adequate for rendered pages where
    every forbidden ESID/title would appear as visible text or a data
    attribute.

    ``app_mounted`` controls URL prefix handling.
    """
    status, text = get_raw(session, base_url, path, app_mounted=app_mounted)
    if status is None:
        return errored(str(text))
    if status != expect_status:
        return errored(f"unexpected HTTP {status} from {path}")
    if not isinstance(text, str):
        return errored(f"no body from {path}")
    found = sorted({f for f in forbidden if f and f in text})
    if found:
        return leaked(f"forbidden identifiers in {path} HTML: {found}")
    return passed(f"{path} clean")


def check_denied(
    session: requests.Session,
    base_url: str,
    path: str,
    *,
    allowed_statuses: tuple[int, ...] = (302, 401, 403, 404),
    app_mounted: bool = True,
) -> CheckOutcome:
    """Check that an endpoint refuses access (redirect / 401 / 403 / 404).

    Used for personas that should not reach a surface at all — e.g.
    bare/org_user/team_user/grant_user against /user-management
    (no mc_user_management permission).

    ``app_mounted`` controls URL prefix handling.
    """
    status, _ = get_raw(session, base_url, path, app_mounted=app_mounted)
    if status is None:
        return errored("request failed")
    if status in allowed_statuses:
        return passed(f"{path} HTTP {status} (denied as expected)")
    return leaked(f"{path} returned HTTP {status} — expected denial")


def _extract_csrf_token(session: requests.Session, base_url: str) -> str | None:
    """Extract the CSRF token for an authenticated session so callers
    can issue state-changing requests.

    History: earlier this helper re-fetched ``GET /login``
    expecting the form's hidden ``name="csrf_token"`` input. That
    only worked for unauthenticated sessions — an authenticated GET
    to ``/login`` redirects to ``/dashboard`` (analytics/routes.py:269),
    which renders the token in a ``<meta name="csrf-token">`` tag
    instead. The result was 5 personas × 6 mutate routes = 30
    "CSRF token unavailable" ERRORs in the attack-surface matrix.

    Strategy: hit an authenticated page (``/dashboard``) and parse
    the meta tag. Fall back to the hidden-input form for the
    pre-auth session path (other callers may still need it). The
    session cookie binds CSRF state, so the token from either
    surface is valid for the same session's subsequent POSTs.

    Returns None on extract failure; callers treat that as ERROR.
    """
    import re

    # Authenticated path — meta tag in base_v2.html
    try:
        r = session.get(f"{base_url}/dashboard", allow_redirects=True, timeout=10)
    except requests.RequestException:
        r = None
    if r is not None and r.status_code == 200:
        m = re.search(
            r'<meta[^>]+name=[\'"]csrf-token[\'"][^>]+content=[\'"]([^\'"]+)[\'"]',
            r.text,
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content=[\'"]([^\'"]+)[\'"][^>]+name=[\'"]csrf-token[\'"]',
                r.text,
            )
        if m:
            return m.group(1)

    # Fallback — login form's hidden input (unauthenticated session)
    try:
        r = session.get(f"{base_url}/login", allow_redirects=True, timeout=10)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    match = re.search(r'name=[\'"]csrf_token[\'"]\s+value=[\'"]([^\'"]+)[\'"]', r.text)
    if not match:
        match = re.search(r'value=[\'"]([^\'"]+)[\'"]\s+name=[\'"]csrf_token[\'"]', r.text)
    return match.group(1) if match else None


def post_request(
    session: requests.Session,
    base_url: str,
    path: str,
    *,
    json_body: dict | None = None,
    csrf_token: str | None = None,
    app_mounted: bool = True,
):
    """POST a JSON body with the session's CSRF token.

    Used by the mutate-route attack-surface checks. The CSRF token
    must be passed explicitly so callers can extract it once per
    session and reuse across multiple POSTs without re-fetching
    /login every time.

    Returns (status_code, response_text). Status is None on transport
    error.
    """
    url = f"{base_url}{_resolve_path(path, app_mounted=app_mounted)}"
    headers = {"Content-Type": "application/json"}
    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token
    try:
        r = session.post(
            url,
            json=json_body or {},
            headers=headers,
            allow_redirects=False,
            timeout=15,
        )
    except requests.RequestException as e:
        return None, f"request failed: {e}"
    return r.status_code, r.text


def delete_request(
    session: requests.Session,
    base_url: str,
    path: str,
    *,
    csrf_token: str | None = None,
    app_mounted: bool = True,
):
    """DELETE with the session's CSRF token. Same shape as ``post_request``."""
    url = f"{base_url}{_resolve_path(path, app_mounted=app_mounted)}"
    headers = {}
    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token
    try:
        r = session.delete(url, headers=headers, allow_redirects=False, timeout=15)
    except requests.RequestException as e:
        return None, f"request failed: {e}"
    return r.status_code, r.text


def check_status_in(
    actual_status: int | None,
    expected_statuses: tuple[int, ...],
    *,
    path: str,
    persona_name: str,
    expected_label: str,
) -> CheckOutcome:
    """Assert ``actual_status`` is one of ``expected_statuses``.

    Used by the mutate-route checks: a workspace_admin's POST to a
    cross-workspace target must return 403; a bare/org/team/grant
    persona's POST to ANY target must return 401/403 (denied by the
    _require_admin gate before scope ever applies).

    The ``expected_label`` is a human-readable description for the
    matrix detail ("403 for cross-workspace target", "denied by
    _require_admin", etc.).
    """
    if actual_status is None:
        return errored(f"{path}: request failed")
    if actual_status in expected_statuses:
        return passed(f"{path}: HTTP {actual_status} ({expected_label})")
    return leaked(
        f"{path}: HTTP {actual_status} (expected {expected_label} = " f"{','.join(str(s) for s in expected_statuses)})"
    )


def check_empty_lists(
    session: requests.Session,
    base_url: str,
    path: str,
    *,
    list_keys: tuple[str, ...],
    params: dict[str, Any] | None = None,
    app_mounted: bool = True,
) -> CheckOutcome:
    """Check that named list keys in a JSON response are empty.

    Used for the bare persona where the deny-all floor means filter
    dropdowns / asset lists must be empty, not narrowed.

    ``app_mounted`` controls URL prefix handling.
    """
    params = params or {}
    status, body = get_json(session, base_url, path, app_mounted=app_mounted, **params)
    if status is None:
        return errored(str(body))
    if status != 200:
        return errored(f"unexpected HTTP {status} from {path}")
    if body is None:
        return errored(f"non-JSON response from {path}")
    non_empty: list[str] = []
    for key in list_keys:
        value = _walk_to_key(body, key)
        if isinstance(value, list) and value:
            non_empty.append(f"{key}={len(value)} items")
    if non_empty:
        return leaked(f"expected empty lists at {path} but found: {non_empty}")
    return passed(f"{path} returned empty lists as expected")


def check_title_count_in_json_list(
    session: requests.Session,
    base_url: str,
    path: str,
    *,
    list_key: str,
    title: str,
    expected_count: int,
    params: dict[str, Any] | None = None,
    app_mounted: bool = True,
) -> CheckOutcome:
    """assert ``title`` appears exactly ``expected_count`` times in
    the ``list_key`` list of a JSON response.

    The same-title cross-workspace collision is invisible to a plain
    forbidden-title check: the persona's own team and the foreign
    workspace's team render the IDENTICAL title string, so "title not in
    response" can never fail. The leak instead shows up as a DUPLICATE —
    two "Acme" entries where the persona should see only their own. This
    check counts list entries whose ``title`` equals ``title`` and fails
    if the count exceeds (or falls short of) ``expected_count``.

    A count above expected means the foreign same-titled entry leaked in
    (the exact bug). A count below expected means the persona's
    own entry was wrongly dropped (over-scoping regression).
    """
    params = params or {}
    status, body = get_json(session, base_url, path, app_mounted=app_mounted, **params)
    if status is None:
        return errored(str(body))
    if status != 200:
        return errored(f"unexpected HTTP {status} from {path}")
    if body is None:
        return errored(f"non-JSON response from {path}")
    items = _walk_to_key(body, list_key)
    if not isinstance(items, list):
        return errored(f"no list at key '{list_key}' in {path} response")
    count = sum(1 for it in items if isinstance(it, dict) and it.get("title") == title)
    if count > expected_count:
        return leaked(
            f"{path}: title '{title}' appeared {count}x (expected {expected_count}) "
            f"— foreign same-title entry leaked"
        )
    if count < expected_count:
        return errored(
            f"{path}: title '{title}' appeared {count}x (expected {expected_count}) "
            f"— persona's own entry was dropped (over-scoping)"
        )
    return passed(f"{path}: title '{title}' appeared exactly {expected_count}x as expected")


def _walk_to_key(obj: Any, key: str) -> Any:
    """Find first occurrence of ``key`` in a nested dict; return None if absent."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _walk_to_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _walk_to_key(v, key)
            if found is not None:
                return found
    return None
