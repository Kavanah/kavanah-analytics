"""Fixtures for live scope-isolation and attack-surface tests.

These tests hit a real deployed environment (staging recommended) and are
marked ``live`` so they are skipped in CI.

Two suites share this conftest:

1. ``test_scope_isolation.py`` — original single-user URL-tampering regression
   . Uses a single pasted session cookie. Env vars:
   TEST_BASE_URL, TEST_SESSION_COOKIE, TEST_ALLOWED_TEAM, TEST_FORBIDDEN_TEAM,
   TEST_FORBIDDEN_ORG, TEST_SCOPED_DASHBOARD_ID, TEST_HIDDEN_DASHBOARD_ID.

2. ``test_attack_surface.py`` — multi-persona regression across the
    attack-surface inventory. Six personas (bare, workspace_admin,
   org_user, team_user, grant_user, superuser) log in via scripted POST
   to /login. Each persona has its own credential + expected-scope env
   vars (PERSONA_<NAME>_USERNAME, PERSONA_<NAME>_PASSWORD, plus
   allowed/forbidden identifiers). See PERSONAS.md for the one-time
   staging account setup.

The two suites coexist: a missing env var only skips the suite that
needs it, not the other.

Both suites refuse to run against the production hostname.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Iterator

import pytest
import requests


# ---------------------------------------------------------------------------
# Production-hostname guard
# ---------------------------------------------------------------------------

# one source of truth for the prod list (tests/conftest.py puts bin/python
# on sys.path first); extend with PROD_HOSTNAMES for legacy aliases.
from lib.prod_hosts import prod_hostnames  # noqa: E402


def _refuse_prod(base_url: str) -> None:
    """Hard-fail if base_url resolves to the production hostname.

    The attack-surface personas were originally staging-only test accounts.
    Running this suite against prod would either fail (accounts don't exist)
    or, worse, exercise prod auth with throwaway credentials.

     stood the suite up on prod with dedicated throwaway personas, so
    the guard now has an explicit opt-in: set ``ATTACK_SURFACE_ALLOW_PROD=1``
    to permit a prod hostname. The runner script (run-attack-surface-tests.sh)
    only exports that var when ``--prod`` is passed, and CI never sets it — so
    the default for any accidental/CI run is still a hard refusal. Both the
    runner flag and this env var must agree before prod is reachable.
    """
    if os.environ.get("ATTACK_SURFACE_ALLOW_PROD") == "1":
        return
    lower = base_url.lower()
    for host in prod_hostnames():
        if host in lower:
            pytest.fail(
                f"Refusing to run live security tests against production ({host}). "
                f"Set TEST_BASE_URL to a staging host, or pass --prod to opt in."
            )


# ---------------------------------------------------------------------------
# Original scope-isolation fixtures (test_scope_isolation.py)
# ---------------------------------------------------------------------------

_SCOPE_ISOLATION_REQUIRED_ENV = [
    "TEST_BASE_URL",
    "TEST_SESSION_COOKIE",
    "TEST_ALLOWED_TEAM",
    "TEST_FORBIDDEN_TEAM",
    "TEST_FORBIDDEN_ORG",
    "TEST_SCOPED_DASHBOARD_ID",
    "TEST_HIDDEN_DASHBOARD_ID",
]


@pytest.fixture(scope="session")
def live_config() -> dict:
    missing = [k for k in _SCOPE_ISOLATION_REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        pytest.skip(f"live tests skipped — missing env vars: {', '.join(missing)}")
    base_url = os.environ["TEST_BASE_URL"].rstrip("/")
    _refuse_prod(base_url)
    return {
        "base_url": base_url,
        "session_cookie": os.environ["TEST_SESSION_COOKIE"],
        "allowed_team": os.environ["TEST_ALLOWED_TEAM"],
        "forbidden_team": os.environ["TEST_FORBIDDEN_TEAM"],
        "forbidden_org": os.environ["TEST_FORBIDDEN_ORG"],
        "scoped_dashboard_id": os.environ["TEST_SCOPED_DASHBOARD_ID"],
        "hidden_dashboard_id": os.environ["TEST_HIDDEN_DASHBOARD_ID"],
        "public_dashboard_id": os.environ.get("TEST_PUBLIC_DASHBOARD_ID", "b451702a-6ac3-46a3-9a40-6768130afc7f"),
    }


@pytest.fixture(scope="session")
def live_session(live_config: dict) -> requests.Session:
    s = requests.Session()
    s.cookies.set("session", live_config["session_cookie"])
    s.headers.update(
        {
            "Accept": "*/*",
            "User-Agent": "scope-isolation-tests/1.0",
        }
    )
    return s


# ---------------------------------------------------------------------------
# Attack-surface persona fixtures (test_attack_surface.py)
# ---------------------------------------------------------------------------


PERSONA_NAMES = (
    "bare",
    "workspace_admin",
    "org_user",
    "team_user",
    "grant_user",
    "superuser",
    # expansion (2026-05-26): four cohort shapes that the original
    # six personas miss. Each has a `de_authz_user_*_role` row but NO
    # membership doc — the new-authz cohort that
    # surfaced. Live positive controls on the resolver fixes from those
    # tickets. See `the private docs`
    # for the full shape table; PERSONAS.md for staging-account provisioning.
    "team_grant",
    "workspace_member",
    "org_grant",
    "workspace_grant",
)


@dataclass
class Persona:
    """A test persona for the attack-surface suite.

    ``allowed_*`` / ``forbidden_*`` identifiers are titles (org / team)
    or workspace slugs. Assertions check that responses contain only
    ``allowed_*`` values and never any ``forbidden_*`` value.

    ``can_see_user_management`` reflects whether the persona has the
    ``mc_user_management`` permission. Used to assert 403/redirect for
    personas that should NOT reach that page.
    """

    name: str
    username: str
    password: str
    base_url: str
    session: requests.Session = field(default=None)  # type: ignore[assignment]
    allowed_orgs: tuple[str, ...] = ()
    forbidden_orgs: tuple[str, ...] = ()
    allowed_teams: tuple[str, ...] = ()
    forbidden_teams: tuple[str, ...] = ()
    allowed_workspaces: tuple[str, ...] = ()
    forbidden_workspaces: tuple[str, ...] = ()
    # connector-asset TITLES this persona must never see in a
    # pick-existing dropdown (Echo tenant / Chatwoot account / Google Ads
    # customer / GA4 property / FB account|app|app_user|page). Distinct
    # from forbidden_orgs/teams/workspaces because the picker leaks asset
    # titles, which are NOT org/team/workspace identifiers. Populate with
    # the titles of the control-workspace's connector assets on staging.
    forbidden_asset_titles: tuple[str, ...] = ()
    is_superuser: bool = False
    can_see_user_management: bool = False
    # For the no-scope deny-all persona (bare): asserts that filter dropdowns
    # and asset lists return empty rather than narrowed.
    expects_empty_scope: bool = False
    # For the grant_user persona: the team(s) the workspace-scoped grant
    # should resolve to. Distinct from allowed_teams because the grant
    # path is separate from membership.
    granted_teams: tuple[str, ...] = ()
    # a team TITLE that this persona's own allowed team shares with
    # a DIFFERENT workspace's team (the same-title collision). The leak
    # this guards is invisible to a plain forbidden-title check — both
    # teams render the identical title string — so the collision test
    # asserts on the COUNT of that title in a response instead (the
    # persona must see exactly ONE "Acme", their own, not the foreign
    # workspace's same-titled team). Populate with the colliding title;
    # leave empty to skip the collision suite for this persona. The
    # matching foreign team must exist in a workspace this persona has no
    # scope on — see PERSONAS.md "Same-title collision" setup.
    same_title_team: str | None = None
    # How many times ``same_title_team`` should appear in a scoped response
    # for this persona — almost always 1 (their own). Configurable so a
    # multi-team persona that legitimately owns two same-titled teams in
    # its OWN workspace(s) can pin the correct count.
    same_title_expected_count: int = 1


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


def _csv(name: str) -> tuple[str, ...]:
    val = os.environ.get(name, "")
    return tuple(s.strip() for s in val.split(",") if s.strip())


def _load_persona(name: str, base_url: str) -> Persona | None:
    """Read persona credentials + expected scope from env.

    Returns None if either USERNAME or PASSWORD is missing (the persona
    is then skipped from the matrix rather than failing the suite).
    """
    prefix = f"PERSONA_{name.upper()}"
    username = _env(f"{prefix}_USERNAME")
    password = _env(f"{prefix}_PASSWORD")
    if not username or not password:
        return None
    return Persona(
        name=name,
        username=username,
        password=password,
        base_url=base_url,
        allowed_orgs=_csv(f"{prefix}_ALLOWED_ORGS"),
        forbidden_orgs=_csv(f"{prefix}_FORBIDDEN_ORGS"),
        allowed_teams=_csv(f"{prefix}_ALLOWED_TEAMS"),
        forbidden_teams=_csv(f"{prefix}_FORBIDDEN_TEAMS"),
        allowed_workspaces=_csv(f"{prefix}_ALLOWED_WORKSPACES"),
        forbidden_workspaces=_csv(f"{prefix}_FORBIDDEN_WORKSPACES"),
        forbidden_asset_titles=_csv(f"{prefix}_FORBIDDEN_ASSET_TITLES"),
        is_superuser=(_env(f"{prefix}_IS_SUPERUSER", "false") or "").lower() == "true",
        can_see_user_management=((_env(f"{prefix}_CAN_SEE_USER_MANAGEMENT", "false") or "").lower() == "true"),
        expects_empty_scope=((_env(f"{prefix}_EXPECTS_EMPTY_SCOPE", "false") or "").lower() == "true"),
        granted_teams=_csv(f"{prefix}_GRANTED_TEAMS"),
        same_title_team=_env(f"{prefix}_SAME_TITLE_TEAM"),
        same_title_expected_count=int(_env(f"{prefix}_SAME_TITLE_EXPECTED_COUNT", "1") or "1"),
    )


def _answer_mfa_challenge(s: requests.Session, base_url: str, username: str, shared_secret: str, initial_location: str) -> str:
    """(2026-05-26): answer the /mfa/challenge intercept with the
    current code derived from ``shared_secret``.

    Returns the post-challenge Location header so the caller can
    continue its existing redirect-validation logic. On any failure
    (challenge GET non-200, missing CSRF token, wrong code, no
    redirect on POST) calls ``pytest.fail`` with enough context to
    debug — these are setup errors, not silent skips.

    The session ``s`` already carries the ``mfa_pending`` cookie state
    set by the credential POST that returned ``initial_location``.
    Same session is mutated in place with the post-challenge auth
    cookie on success.
    """
    import re

    try:
        import pyotp
    except ImportError:
        pytest.fail(
            f"PERSONA_SHARED_MFA_SECRET is set but pyotp is not importable in the "
            f"test runner. Install with `pip install pyotp` (already a runtime "
            f"dependency of mfa_service.py)."
        )

    # Fetch the challenge page to grab a fresh CSRF token. Follow
    # redirects: a non-prefixed /mfa/challenge may bounce to the
    # nginx-prefixed path on staging/prod.
    challenge_url = f"{base_url}/mfa/challenge"
    try:
        get_resp = s.get(challenge_url, allow_redirects=True, timeout=10)
    except requests.RequestException as e:
        pytest.fail(f"GET /mfa/challenge for {username} failed: {e}")
    if get_resp.status_code != 200:
        pytest.fail(
            f"GET /mfa/challenge for {username} returned HTTP {get_resp.status_code} "
            f"(expected 200). The mfa_pending session state may have expired. "
            f"Response snippet: {get_resp.text[:200]}"
        )

    # Same CSRF-token extraction shape as the /login form.
    csrf_match = re.search(r'name=[\'"]csrf_token[\'"]\s+value=[\'"]([^\'"]+)[\'"]', get_resp.text)
    if not csrf_match:
        csrf_match = re.search(r'value=[\'"]([^\'"]+)[\'"]\s+name=[\'"]csrf_token[\'"]', get_resp.text)
    if not csrf_match:
        pytest.fail(f"Could not extract CSRF token from /mfa/challenge for {username}. " f"Challenge form may have changed.")
    csrf_token = csrf_match.group(1)

    # Compute the current code from the shared seed. Deterministic
    # function of (seed, current 30-second time window) — the same
    # function an authenticator app runs.
    try:
        code = pyotp.TOTP(shared_secret).now()
    except Exception as e:  # noqa: BLE001 — boundary
        pytest.fail(
            f"pyotp.TOTP failed for {username}: {e}. " f"Check that PERSONA_SHARED_MFA_SECRET is a valid base32 string."
        )

    try:
        post_resp = s.post(
            challenge_url,
            data={"code": code, "csrf_token": csrf_token},
            allow_redirects=False,
            timeout=10,
        )
    except requests.RequestException as e:
        pytest.fail(f"POST /mfa/challenge for {username} failed: {e}")

    # Success: 302 to the post-MFA destination (typically /dashboard).
    # Failure: 200 with the challenge page re-rendered + error_message.
    # That happens when the seed doesn't match what's stored on the
    # account, the form re-renders rather than redirecting.
    if post_resp.status_code != 302:
        pytest.fail(
            f"MFA challenge for {username} returned HTTP {post_resp.status_code} "
            f"(expected 302). The shared seed may not match the secret seeded on "
            f"this account in User Management. Re-seed the account with "
            f"PERSONA_SHARED_MFA_SECRET. Response snippet: {post_resp.text[:200]}"
        )
    return post_resp.headers.get("Location", "")


def _login(base_url: str, username: str, password: str) -> requests.Session:
    """POST credentials to /login and return a session with the auth cookie.

    Raises pytest.fail (not skip) on auth failure — credentials being
    wrong is a setup error the operator must see, not a silent skip.

    The login form requires a CSRF token (analytics/routes.py:305) and
    the token is rendered into the GET /login page's hidden input.
    Both failed CSRF and successful auth return 302; the difference is
    the Location header (login-page vs dashboard). We inspect Location
    after POST to disambiguate.
    """
    import re

    s = requests.Session()
    s.headers.update(
        {
            "Accept": "*/*",
            "User-Agent": "attack-surface-tests/1.0",
        }
    )
    # GET the login page to receive the session cookie + CSRF token.
    # Follow redirects on this GET — some deployments serve /login from
    # /assets/login and bounce; we want to land on the actual form.
    get_resp = s.get(f"{base_url}/login", allow_redirects=True, timeout=10)
    if get_resp.status_code != 200:
        pytest.fail(
            f"GET /login failed for {username}: HTTP {get_resp.status_code}. "
            f"Cannot retrieve CSRF token. Response snippet: {get_resp.text[:200]}"
        )

    # Extract CSRF token from the rendered form. The hidden input is
    # name="csrf_token" value="..."; tolerate either single- or
    # double-quoted attributes.
    csrf_match = re.search(r'name=[\'"]csrf_token[\'"]\s+value=[\'"]([^\'"]+)[\'"]', get_resp.text)
    if not csrf_match:
        # Fallback: some templates render value before name.
        csrf_match = re.search(r'value=[\'"]([^\'"]+)[\'"]\s+name=[\'"]csrf_token[\'"]', get_resp.text)
    if not csrf_match:
        pytest.fail(f"Could not extract CSRF token from /login for {username}. " f"Login form may have changed.")
    csrf_token = csrf_match.group(1)

    resp = s.post(
        f"{base_url}/login",
        data={"username": username, "password": password, "csrf_token": csrf_token},
        allow_redirects=False,
        timeout=10,
    )
    # Both failed CSRF/credentials and successful auth return 302. The
    # Location header disambiguates:
    #   * Success: → "/dashboard", "/?show=dashboard", "/" etc.
    #   * Failed CSRF or bad credentials: → "/login" (possibly with ?error=...)
    #   * MFA intercept (bypass not set): → "/mfa/challenge" or "/mfa/setup"
    #   * Force password change: → "/change-password"
    if resp.status_code != 302:
        pytest.fail(
            f"Login POST for {username} returned HTTP {resp.status_code} "
            f"(expected 302). Response snippet: {resp.text[:200]}"
        )
    location = resp.headers.get("Location", "")
    if "/login" in location:
        pytest.fail(
            f"Login for {username} redirected back to {location!r} — "
            f"credentials rejected. Check the password in .env.attack-surface "
            f"matches the password you set when rotating from the temp."
        )
    if "/mfa/" in location:
        # (2026-05-26): exercise the real second-factor flow
        # when a shared base32 seed is provisioned in the env. Each
        # persona account is seeded with the same secret at user-create
        # via the User Management UI's admin-override path. Falls
        # back to today's "set break-glass" message when the env var
        # is absent so existing bypass-based setups keep working until
        # the seed is rolled out. See PERSONAS.md for the full
        # provisioning + rotation procedure.
        shared_secret = os.environ.get("PERSONA_SHARED_MFA_SECRET")
        if not shared_secret:
            pytest.fail(
                f"Login for {username} hit MFA flow at {location!r}. "
                f"Either set bypass_mfa=true on this account in User Management, "
                f"or set PERSONA_SHARED_MFA_SECRET in .env.attack-surface and "
                f"seed each persona account with that secret."
            )
        location = _answer_mfa_challenge(s, base_url, username, shared_secret, location)
    if "change-password" in location or "force" in location:
        pytest.fail(
            f"Login for {username} requires password change ({location!r}). "
            f"Rotate the password manually via the UI first, then update "
            f".env.attack-surface to match."
        )
    if "session" not in s.cookies:
        pytest.fail(
            f"Login for {username} succeeded ({location!r}) but no 'session' " f"cookie was set. Auth flow may have changed."
        )
    return s


@pytest.fixture(scope="session")
def attack_surface_base_url() -> str:
    base_url = _env("TEST_BASE_URL")
    if not base_url:
        pytest.skip("attack-surface tests skipped — TEST_BASE_URL not set")
    base_url = base_url.rstrip("/")
    _refuse_prod(base_url)
    return base_url


@pytest.fixture(scope="session")
def forbidden_team_esid() -> str | None:
    """esid of an out-of-workspace team (Team B1, workspace-b) the
    A-scoped personas must never be able to configure or read.

    The connector configure gate + the NLQ scope filter both key off the team
    ESID, not a title, so the configure-scope and NLQ-scope surfaces POST this
    raw esid. Emitted by ``seed_attack_surface_personas.py`` as
    ``TEST_FORBIDDEN_TEAM_ESID``. Returns None when unset so those surfaces
    SKIP (a missing target must never read as a green) rather than ERROR.
    """
    return _env("TEST_FORBIDDEN_TEAM_ESID")


@pytest.fixture(scope="session")
def persona_csrf_tokens(personas: dict, attack_surface_base_url: str) -> dict[str, str]:
    """Pre-fetch a CSRF token for each persona's session.

    Mutate-route attack-surface checks need a
    ``X-CSRF-Token`` header. We extract the token once per persona at
    session start so the test functions don't re-GET /login on every
    POST. The session cookie is the binding; the token can be reused
    across multiple POSTs for the lifetime of the session.

    Returns ``{persona_name: csrf_token}``. Personas whose login
    session can't yield a token are silently omitted — callers that
    need the token check ``persona.name in persona_csrf_tokens``
    before issuing the POST and skip otherwise (rare; almost always a
    login flow regression that's better surfaced elsewhere).
    """
    from .attack_surface_helpers import _extract_csrf_token

    tokens: dict[str, str] = {}
    for name, persona in personas.items():
        token = _extract_csrf_token(persona.session, attack_surface_base_url)
        if token:
            tokens[name] = token
    return tokens


@pytest.fixture(scope="session")
def personas(attack_surface_base_url: str) -> dict[str, Persona]:
    """Load all configured personas and log each one in.

    Personas with missing credentials are silently omitted from the
    returned dict. Tests parametrize over the loaded set, so an
    operator can run the suite with a subset of personas.
    """
    loaded: dict[str, Persona] = {}
    for name in PERSONA_NAMES:
        p = _load_persona(name, attack_surface_base_url)
        if p is None:
            continue
        p.session = _login(attack_surface_base_url, p.username, p.password)
        loaded[name] = p
    if not loaded:
        pytest.skip(
            "attack-surface tests skipped — no PERSONA_*_USERNAME/PASSWORD "
            "pairs configured. See tests/security/PERSONAS.md."
        )
    return loaded


# ---------------------------------------------------------------------------
# Matrix readout plugin
# ---------------------------------------------------------------------------
#
# Each attack-surface test records its outcome via the ``surface_result``
# fixture. At end of session we print a persona × surface matrix.


@dataclass
class SurfaceResult:
    persona: str
    surface: str
    outcome: str  # PASS | LEAK | XFAIL | XPASS | ERROR | SKIP
    detail: str = ""


_SURFACE_RESULTS: list[SurfaceResult] = []


def record_surface_result(persona: str, surface: str, outcome: str, detail: str = "") -> None:
    """Test helper: append a row to the matrix."""
    _SURFACE_RESULTS.append(SurfaceResult(persona=persona, surface=surface, outcome=outcome, detail=detail))


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Print the attack-surface matrix at the end of the test session.

    Only runs when at least one surface result was recorded (i.e. the
    attack-surface suite actually executed). Output is a fixed-width
    table to stdout via the terminal reporter.
    """
    if not _SURFACE_RESULTS:
        return

    personas_seen: list[str] = []
    surfaces_seen: list[str] = []
    grid: dict[tuple[str, str], SurfaceResult] = {}
    for r in _SURFACE_RESULTS:
        if r.persona not in personas_seen:
            personas_seen.append(r.persona)
        if r.surface not in surfaces_seen:
            surfaces_seen.append(r.surface)
        grid[(r.surface, r.persona)] = r

    surface_col_width = max(len("surface"), max(len(s) for s in surfaces_seen))
    persona_col_width = max(
        len("outcome"),
        max(len(p) for p in personas_seen),
        len("XFAIL"),
    )

    terminalreporter.write_sep("=", "ATTACK SURFACE MATRIX")
    header = "surface".ljust(surface_col_width) + "  " + "  ".join(p.ljust(persona_col_width) for p in personas_seen)
    terminalreporter.write_line(header)
    terminalreporter.write_line("-" * len(header))
    for surface in surfaces_seen:
        row_cells = []
        for persona in personas_seen:
            r = grid.get((surface, persona))
            row_cells.append((r.outcome if r else "-").ljust(persona_col_width))
        terminalreporter.write_line(surface.ljust(surface_col_width) + "  " + "  ".join(row_cells))

    # Summary counts
    counts: dict[str, int] = {}
    for r in _SURFACE_RESULTS:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    terminalreporter.write_sep("-", "summary")
    terminalreporter.write_line(summary)

    # Detail lines for anything not PASS / XFAIL
    interesting = [r for r in _SURFACE_RESULTS if r.outcome not in ("PASS", "XFAIL")]
    if interesting:
        terminalreporter.write_sep("-", "non-pass detail")
        for r in interesting:
            terminalreporter.write_line(f"  [{r.outcome}] {r.persona} / {r.surface}: {r.detail}")
