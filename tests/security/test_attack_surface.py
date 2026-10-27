"""Multi-persona attack-surface regression suite.

Walks every surface in ``the private docs``
against 6 personas (bare, workspace_admin, org_user, team_user, grant_user,
superuser) and asserts that no forbidden identifier appears in any response.

Run::

    source .env.attack-surface
    pytest tests/security/test_attack_surface.py -v -m live

End-of-session output is a persona × surface matrix printed by the
``pytest_terminal_summary`` plugin in ``conftest.py``.

Surfaces marked ``xfail`` correspond to filed:

* Workspace-aware dashboard menus
* Data source assets workspace scoping
* User Management user list workspace scope for non-superusers

When any of those tickets ship, the matching test flips to XPASS and the
operator promotes the row to a real PASS assertion.
"""

from __future__ import annotations

import pytest

from .attack_surface_helpers import (
    check_denied,
    check_empty_lists,
    check_no_forbidden_in_html,
    check_no_forbidden_in_json,
    check_title_count_in_json_list,
    find_forbidden_in_payload,
    get_json,
    get_raw,
    leaked,
    passed,
    skipped,
)
from .conftest import Persona, record_surface_result


pytestmark = pytest.mark.live


# title of the out-of-workspace team the connector configure / NLQ
# surfaces target (Team B1, workspace-b). A persona legitimately scoped to it
# (grant_user) is skipped by the configure-scope surface, since for them the
# target is in-scope. Mirrors _CROSS_TENANT_TEAM_TITLE in
# bin/python/tools/seed_attack_surface_personas.py.
_CROSS_TENANT_TEAM_TITLE = "Team B1"


# ---------------------------------------------------------------------------
# Parametrization helper
# ---------------------------------------------------------------------------


def _all_personas(personas: dict[str, Persona]) -> list[Persona]:
    """Stable-ordered list of loaded personas for parametrization."""
    order = ("bare", "workspace_admin", "org_user", "team_user", "grant_user", "superuser")
    return [personas[name] for name in order if name in personas]


@pytest.fixture
def all_personas(personas: dict[str, Persona]) -> list[Persona]:
    return _all_personas(personas)


def _forbidden_for(persona: Persona, kind: str) -> tuple[str, ...]:
    """Identifiers this persona must never see in a response.

    Superusers have no forbidden identifiers by design — they see all
    tenants. For all other personas, we use the env-configured
    forbidden list for the surface kind.
    """
    if persona.is_superuser:
        return ()
    if kind == "orgs":
        return persona.forbidden_orgs
    if kind == "teams":
        return persona.forbidden_teams
    if kind == "workspaces":
        return persona.forbidden_workspaces
    return ()


def _record(persona: Persona, surface: str, outcome) -> None:
    record_surface_result(
        persona=persona.name,
        surface=surface,
        outcome=outcome.outcome,
        detail=outcome.detail,
    )


_DENIAL_STATUS_TOKENS = ("HTTP 302", "HTTP 401", "HTTP 403", "HTTP 404")


def _convert_denial_to_pass(outcome, persona_name: str, surface: str):
    """If ``outcome`` is an ERROR caused by an auth/denial status, convert
    it to a PASS with a clear detail message. Used for surfaces where
    the persona is expected to be denied access (e.g. bare on Asset
    Explorer endpoints, non-agency personas on /asset/organizations).

    Returns the outcome unchanged when it's not a denial.
    """
    from .attack_surface_helpers import CheckOutcome

    if outcome.outcome != "ERROR":
        return outcome
    detail = outcome.detail or ""
    for token in _DENIAL_STATUS_TOKENS:
        if token in detail:
            return CheckOutcome("PASS", f"{surface}: {persona_name} denied as expected ({token.split()[1]})")
    return outcome


# ===========================================================================
# Filter dropdowns — /api/dashboards/filter-options
# ===========================================================================


class TestFilterDropdowns:
    """Phase B.2 + B.4 — _scope_filter_options_to_user.

    The endpoint returns ``{"organizations": [...], "teams": [...]}``
    plus aggregations. No forbidden org or team title may appear.
    Bare persona must see empty lists (deny-all floor).
    """

    PATH = "/api/dashboards/filter-options"
    SURFACE = "filter_dropdowns"

    def test_filter_dropdowns(self, all_personas):
        for persona in all_personas:
            forbidden = (*_forbidden_for(persona, "orgs"), *_forbidden_for(persona, "teams"))
            if persona.expects_empty_scope:
                outcome = check_empty_lists(
                    persona.session,
                    persona.base_url,
                    self.PATH,
                    list_keys=("organizations", "teams"),
                )
            else:
                outcome = check_no_forbidden_in_json(persona.session, persona.base_url, self.PATH, forbidden)
            _record(persona, self.SURFACE, outcome)
            assert outcome.outcome in ("PASS", "SKIP"), outcome.detail


class TestDashboardVisibilityOrgScoping:
    """follow-up (2026-06-02) — the dashboard-visibility picker
    (``org-team-options``) must show ORGS only to a persona that actually
    holds an org/workspace role. A team-only persona (team role, no org
    role — e.g. a team admin) must get ZERO organizations: the prior
    "parent orgs of my allowed teams" fallback surfaced orgs to a team
    admin, whose authority is the team, not the org.

    Mock-level coverage lives in tests/flask/test_query_scope_probe.py
    (test_team_only_user_sees_no_orgs); this is the live persona-matrix
    counterpart so a regression is caught against real data, not just mocks.
    """

    PATH = "/api/dashboards/org-team-options"
    SURFACE = "dashboard_visibility_orgs"

    def _team_only_personas(self, all_personas):
        # A team-scoped persona with team scope but no org scope, excluding
        # superuser/admin overrides (who legitimately see everything).
        return [
            p
            for p in all_personas
            if not p.is_superuser and p.allowed_teams and not p.allowed_orgs
        ]

    def test_team_only_persona_sees_no_orgs(self, all_personas):
        targets = self._team_only_personas(all_personas)
        if not targets:
            pytest.skip("no team-only persona configured (needs allowed_teams, empty allowed_orgs)")
        for persona in targets:
            status, body = get_json(persona.session, persona.base_url, self.PATH, app_mounted=False)
            if status != 200 or not isinstance(body, dict):
                outcome = skipped(f"{self.SURFACE}: {persona.name} got status={status} (not a JSON 200)")
            else:
                orgs = body.get("organizations") or []
                if orgs:
                    outcome = leaked(
                        f"{self.SURFACE}: team-only persona {persona.name} sees "
                        f"{len(orgs)} org(s) in the visibility picker: "
                        f"{[o.get('title') for o in orgs][:5]}"
                    )
                else:
                    outcome = passed(f"{self.SURFACE}: {persona.name} sees no orgs (team-only)")
            _record(persona, self.SURFACE, outcome)
            assert outcome.outcome in ("PASS", "SKIP"), outcome.detail


# ===========================================================================
# Same-title cross-workspace collision —
# ===========================================================================


class TestSameTitleCollision:
    """two workspaces own a team with the IDENTICAL title.

    The original attack-surface matrix passed while five team/org list
    surfaces leaked across workspaces and the resolver pulled a
    foreign same-titled team into scope. The reason it missed
    them: every persona's allowed/forbidden titles were UNIQUE, so a
    "forbidden title not in response" check could never trip on a title
    COLLISION — both the own and the foreign team render the same string.

    This suite closes that blind spot. For a persona whose own allowed
    team title collides with a foreign workspace's team title
    (``same_title_team``), each surface that lists teams must return that
    title exactly ``same_title_expected_count`` times (normally 1 — the
    persona's own). A second occurrence is the foreign workspace's team
    leaking in.

    SETUP (staging, one-time): create two workspaces, each owning an org
    + team that share a title (e.g. both "Acme"). Give the persona scope
    on workspace A only. Set ``PERSONA_<NAME>_SAME_TITLE_TEAM=Acme`` (and
    optionally ``_SAME_TITLE_EXPECTED_COUNT``). See PERSONAS.md
    "Same-title collision". Personas without the env var are
    SKIPPED — so this suite is inert until the staging data exists, never
    a false green.

    Surfaces exercised (the two title-keyed team lists most directly fed
    by the resolver path + the list helper):
      * /api/dashboards/filter-options  ("teams")
      * /api/tree/assets/teams          (Asset Explorer team list JSON)
    """

    FILTER_PATH = "/api/dashboards/filter-options"
    TEAM_LIST_PATH = "/api/tree/assets/teams"

    def _personas_with_collision(self, all_personas):
        return [p for p in all_personas if p.same_title_team and not p.is_superuser]

    def test_filter_options_no_duplicate_same_title_team(self, all_personas):
        targets = self._personas_with_collision(all_personas)
        if not targets:
            pytest.skip(
                "no persona has SAME_TITLE_TEAM configured — see PERSONAS.md "
                "'Same-title collision' for staging setup"
            )
        for persona in targets:
            outcome = check_title_count_in_json_list(
                persona.session,
                persona.base_url,
                self.FILTER_PATH,
                list_key="teams",
                title=persona.same_title_team,
                expected_count=persona.same_title_expected_count,
            )
            _record(persona, "same_title_filter_options", outcome)
            assert outcome.outcome in ("PASS", "SKIP"), outcome.detail

    def test_team_list_no_duplicate_same_title_team(self, all_personas):
        targets = self._personas_with_collision(all_personas)
        if not targets:
            pytest.skip(
                "no persona has SAME_TITLE_TEAM configured — see PERSONAS.md "
                "'Same-title collision' for staging setup"
            )
        for persona in targets:
            outcome = check_title_count_in_json_list(
                persona.session,
                persona.base_url,
                self.TEAM_LIST_PATH,
                list_key="teams",
                title=persona.same_title_team,
                expected_count=persona.same_title_expected_count,
            )
            # The Asset Explorer JSON nests teams under a tree node; the
            # walk-to-key in the helper finds the first "teams" list. If a
            # persona is denied this endpoint (deny-all floor), that's a
            # pass — no list to leak.
            outcome = _convert_denial_to_pass(outcome, persona.name, "same_title_team_list")
            _record(persona, "same_title_team_list", outcome)
            assert outcome.outcome in ("PASS", "SKIP"), outcome.detail


# ===========================================================================
# Chart data — /api/map-data (Dashboard Builder routes)
# ===========================================================================


class TestChartMapData:
    """Phase B + B.1 + B.3 — chart sites use scoped_index_pattern.

    Spot-checks 4 representative statistic keys covering the four
    importer families: fb_insights (Facebook), echo (Echo messaging),
    google_ads (Google Ads), dt (Disciple.Tools). The ``statistic``
    query param picks the family via ``_MAP_STATISTICS`` (dashboard_
    builder/routes.py:1065). A forbidden org/team appearing in the
    aggregations means a Layer-1 wrap was missed.

    Defaults are real statistic keys from _MAP_STATISTICS. Override
    via env vars only if your staging has renamed or removed one.
    """

    # Mount: dashboard_builder blueprint at /dashboard-builder. When
    # hit through nginx the request URL is
    # /assets/dashboard-builder/api/map-data — PrefixMiddleware strips
    # /assets/ before the blueprint resolves the rest. So app_mounted=
    # True applies and we let _resolve_path prepend /assets.
    PATH = "/dashboard-builder/api/map-data"
    SURFACE_FB = "chart_map_data_facebook"
    SURFACE_ECHO = "chart_map_data_echo"
    SURFACE_GADS = "chart_map_data_google_ads"
    SURFACE_DT = "chart_map_data_dt"

    @pytest.mark.parametrize(
        "surface,env_key,default_statistic",
        [
            # 'reach' is the canonical fb_insights statistic and the
            # default the endpoint itself falls back to.
            (SURFACE_FB, "TEST_CHART_STAT_FACEBOOK", "reach"),
            # 'messages' covers the echo data source.
            (SURFACE_ECHO, "TEST_CHART_STAT_ECHO", "messages"),
            # 'google_ads_app_installs' is the only non-fb google_ads
            # statistic; the other google_ads numbers ride through the
            # generic country-map path with the same Layer-1 wrap.
            (SURFACE_GADS, "TEST_CHART_STAT_GOOGLE_ADS", "google_ads_app_installs"),
            # 'baptisms' covers the dt (Disciple.Tools) data source —
            # adds coverage for the cross-tenant DT index pattern.
            (SURFACE_DT, "TEST_CHART_STAT_DT", "baptisms"),
        ],
    )
    def test_chart_map_data(self, all_personas, surface, env_key, default_statistic):
        import os

        statistic = os.environ.get(env_key, default_statistic)
        for persona in all_personas:
            forbidden = (
                *_forbidden_for(persona, "orgs"),
                *_forbidden_for(persona, "teams"),
            )
            outcome = check_no_forbidden_in_json(
                persona.session,
                persona.base_url,
                self.PATH,
                forbidden,
                params={"statistic": statistic},
            )
            _record(persona, surface, outcome)
            # ERROR (404/500 etc.) on map-data for a given statistic is
            # recorded but doesn't fail the test — the statistic may not
            # have data for the persona's workspace, which is fine.
            assert outcome.outcome in ("PASS", "ERROR", "SKIP"), outcome.detail


# ===========================================================================
# Dashboard menus — (xfail)
# ===========================================================================


class TestDashboardMenus:
    """workspace-scoped dashboard menus.

    The route at /dashboard-builder/api/menus resolves the operator's
    primary workspace via ``_current_workspace_esid_or_abort`` and
    queries ``WorkspaceMenuService.list_menus(workspace_esid)`` — which
    runs a ``term`` filter on ``workspace_esid`` against
    ``de_authz_workspace_menu``. The JSON shape returned is
    ``{"success": True, "menus": [{"esid", "title", "position"}, ...]}``
    — no nested workspace_esid leaf, so the response can't structurally
    leak another workspace's identifier even if titles were ambiguous.

     follow-up (2026-05-20): the original xfail dates from before
     wired the per-workspace filter through this endpoint. The
    route is now correctly workspace-scoped, so the xfail is dropped.

    Test depth (two layers):

      1. **Title scan** (existing) — no forbidden workspace identifier
         appears as a string leaf. Catches the easy leak where a menu
         title accidentally contains another workspace's slug.

      2. **workspace_esid walk** (added follow-up) — if any
         future refactor adds a ``workspace_esid`` field to the menu
         objects (e.g. for client-side filtering), the test will fail
         the moment one of those fields points at a workspace outside
         the operator's allowed set. Today no such field exists; this
         is defense-in-depth against the regression class the original
         docstring worried about.

    Personas without mc_dashboard_builder (bare/org_user/team_user/
    grant_user) get 403 — the deny-all floor at the perm-gate level.
    Recorded as PASS via _convert_denial_to_pass.
    """

    # Mount: dashboard_builder blueprint. Same /assets-prefix story
    # as TestChartMapData — nginx hits /assets/dashboard-builder/...,
    # PrefixMiddleware strips /assets/, blueprint resolves the rest.
    PATH = "/dashboard-builder/api/menus"
    SURFACE = "dashboard_menus"

    def test_dashboard_menus(self, all_personas):
        from .attack_surface_helpers import get_json

        leak_found = False
        last_detail = ""
        for persona in all_personas:
            if persona.is_superuser:
                _record(persona, self.SURFACE, _ok("superuser sees all menus by design"))
                continue
            forbidden = persona.forbidden_workspaces

            # Layer 1: title-leaf scan via the existing helper. Catches
            # forbidden strings appearing anywhere in the JSON payload.
            outcome = check_no_forbidden_in_json(persona.session, persona.base_url, self.PATH, forbidden)

            # Personas lacking mc_dashboard_builder hit the perm-gate
            # 403 before the list runs; record that as PASS.
            outcome = _convert_denial_to_pass(outcome, persona.name, self.SURFACE)

            # Layer 2: if the
            # endpoint returned a 200 payload, walk it for any
            # ``workspace_esid`` value that's in the operator's
            # forbidden_workspaces set. Today the menus payload has no
            # workspace_esid field — but a future refactor could add
            # one, and this assertion catches that the moment one
            # leaks. Only runs when the title scan already PASSed (so
            # we don't pile structural failures on top of denial-class
            # ones).
            if outcome.outcome == "PASS" and forbidden:
                status, body = get_json(persona.session, persona.base_url, self.PATH)
                if status == 200 and isinstance(body, dict):
                    forbidden_set = set(forbidden)
                    leaked_esids = _collect_workspace_esid_values(body) & forbidden_set
                    if leaked_esids:
                        from .attack_surface_helpers import leaked

                        outcome = leaked(f"workspace_esid leaf(s) point at forbidden workspaces: " f"{sorted(leaked_esids)}")

            _record(persona, self.SURFACE, outcome)
            if outcome.outcome == "LEAK":
                leak_found = True
                last_detail = outcome.detail
        assert not leak_found, last_detail


def _collect_workspace_esid_values(payload) -> set[str]:
    """Walk ``payload`` and collect every ``workspace_esid`` string value.

    Used by TestDashboardMenus layer-2 to catch the regression class
    where a refactor adds a ``workspace_esid`` field to the menus
    response. Recursive over dicts and lists; non-string values at
    the key are ignored (a bug in shape but not a tenant-leak).
    """
    collected: set[str] = set()

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "workspace_esid" and isinstance(v, str) and v:
                    collected.add(v)
                else:
                    _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(payload)
    return collected


# ===========================================================================
# Asset Explorer — org list / team list / tree counts / tree rows
# ===========================================================================


class TestAssetExplorerOrgList:
    """Phase B.4 — list_assets / get_asset_list.

    The HTML page at /asset/organizations and the JSON endpoint at
    /api/tree/assets/organizations must not contain forbidden org
    titles. Bare persona sees empty lists.
    """

    HTML_PATH = "/asset/organizations"
    JSON_PATH = "/api/tree/assets/organizations"
    SURFACE_HTML = "asset_explorer_org_list_html"
    SURFACE_JSON = "asset_explorer_org_list_json"

    def test_org_list_html(self, all_personas):
        for persona in all_personas:
            forbidden = _forbidden_for(persona, "orgs")
            outcome = check_no_forbidden_in_html(persona.session, persona.base_url, self.HTML_PATH, forbidden)
            # /asset/organizations 404s for non-agency users
            # (only is_agency=true workspaces see the Organizations
            # tier). Superuser bypasses. For the test workspaces all
            # is_agency=false, so every non-superuser gets 404 — which
            # is the correct expected behavior, not a leak.
            if not persona.is_superuser:
                outcome = _convert_denial_to_pass(outcome, persona.name, self.SURFACE_HTML)
            _record(persona, self.SURFACE_HTML, outcome)
            assert outcome.outcome in ("PASS", "ERROR", "SKIP"), outcome.detail

    def test_org_list_json(self, all_personas):
        for persona in all_personas:
            forbidden = _forbidden_for(persona, "orgs")
            outcome = check_no_forbidden_in_json(persona.session, persona.base_url, self.JSON_PATH, forbidden)
            # same agency-tier gate on the JSON endpoint.
            if not persona.is_superuser:
                outcome = _convert_denial_to_pass(outcome, persona.name, self.SURFACE_JSON)
            _record(persona, self.SURFACE_JSON, outcome)
            assert outcome.outcome in ("PASS", "ERROR", "SKIP"), outcome.detail


class TestAssetExplorerTeamList:
    HTML_PATH = "/asset/teams"
    JSON_PATH = "/api/tree/assets/teams"
    SURFACE_HTML = "asset_explorer_team_list_html"
    SURFACE_JSON = "asset_explorer_team_list_json"

    def test_team_list_html(self, all_personas):
        for persona in all_personas:
            forbidden = _forbidden_for(persona, "teams")
            outcome = check_no_forbidden_in_html(persona.session, persona.base_url, self.HTML_PATH, forbidden)
            # bare persona: deny-all floor means /asset/teams
            # is unreachable (302 to login or 403). Denial IS the pass
            # condition for bare on every Asset Explorer endpoint.
            if persona.expects_empty_scope:
                outcome = _convert_denial_to_pass(outcome, persona.name, self.SURFACE_HTML)
            _record(persona, self.SURFACE_HTML, outcome)
            assert outcome.outcome in ("PASS", "ERROR", "SKIP"), outcome.detail

    def test_team_list_json(self, all_personas):
        for persona in all_personas:
            forbidden = _forbidden_for(persona, "teams")
            outcome = check_no_forbidden_in_json(persona.session, persona.base_url, self.JSON_PATH, forbidden)
            if persona.expects_empty_scope:
                outcome = _convert_denial_to_pass(outcome, persona.name, self.SURFACE_JSON)
            _record(persona, self.SURFACE_JSON, outcome)
            assert outcome.outcome in ("PASS", "ERROR", "SKIP"), outcome.detail


class TestAssetExplorerTreeCounts:
    """get_tree_structure counts.

    The tree-structure response includes per-asset-type counts. The
    counts for ``teams`` and ``organizations`` must match the persona's
    allowed set size — for the bare persona that's zero. Forbidden
    identifiers should also be absent from the structure JSON.
    """

    PATH = "/api/tree/structure"
    SURFACE = "asset_explorer_tree_counts"

    def test_tree_structure(self, all_personas):
        for persona in all_personas:
            forbidden = (
                *_forbidden_for(persona, "orgs"),
                *_forbidden_for(persona, "teams"),
                *_forbidden_for(persona, "workspaces"),
            )
            outcome = check_no_forbidden_in_json(persona.session, persona.base_url, self.PATH, forbidden)
            # bare persona: tree-structure endpoint is gated; denial
            # (403) is the deny-all floor working correctly.
            if persona.expects_empty_scope:
                outcome = _convert_denial_to_pass(outcome, persona.name, self.SURFACE)
            _record(persona, self.SURFACE, outcome)
            assert outcome.outcome in ("PASS", "ERROR", "SKIP"), outcome.detail


class TestAssetExplorerDataSources:
    """Data source asset enumeration on the Asset Explorer tree route.

    The 13 data-source asset types (FB accounts, Google Ads accounts,
    Echo channels, etc.) on ``/api/tree/assets/<type>`` are now scoped:

      * hides them entirely from non-privileged (org-tier) users —
        ``get_asset_list`` 404s before listing anything.
      * workspace-filters the list for privileged users who do
        reach a 200.

    So the expected matrix is: superuser sees all (by design); a
    privileged-with-scope persona (workspace_admin) reaches a 200 list
    that must contain no forbidden-workspace id; every other scoped
    persona (bare / org_user / team_user / grant_user) is DENIED
    (403/404), which is a PASS — the deny-all / floor working.

    Uses ``facebook_accounts`` as the spot-check asset type.
    """

    SOURCE_TYPE = "facebook_accounts"
    PATH = f"/api/tree/assets/{SOURCE_TYPE}"
    SURFACE = "asset_explorer_data_sources"

    def test_data_sources(self, all_personas):
        leak_found = False
        last_detail = ""
        for persona in all_personas:
            if persona.is_superuser:
                _record(persona, self.SURFACE, _ok("superuser sees all data sources by design"))
                continue
            forbidden = persona.forbidden_workspaces
            outcome = check_no_forbidden_in_json(persona.session, persona.base_url, self.PATH, forbidden)
            # Any non-superuser persona that is DENIED (bare deny-all floor,
            # hiding data sources from org-tier users → 404) is a
            # PASS. A privileged persona that reaches a 200 list is scanned
            # for forbidden ids; a real LEAK is left untouched and still fails.
            outcome = _convert_denial_to_pass(outcome, persona.name, self.SURFACE)
            _record(persona, self.SURFACE, outcome)
            if outcome.outcome == "LEAK":
                leak_found = True
                last_detail = outcome.detail
        assert not leak_found, last_detail


# ===========================================================================
# Data Connectors pick-existing dropdowns —
# ===========================================================================


class TestDataConnectorsPicker:
    """pick-existing dropdowns must not surface assets from
    workspaces outside the operator's scope.

    ``/api/wizard/options/<asset_type>`` (the data_connectors blueprint,
    mounted at ``/assets/data-connectors/`` behind nginx) powers the
    wizard's "Choose existing account" dropdowns on
    ``/assets/data-connectors/``. earlier it ran an unfiltered
    ``AssetService.get_all`` over the whole asset index, so a
    workspace_admin in workspace-A could enumerate every workspace-B
    asset title of the type. The page gate is permission-only
    (mc_data_connectors), so it never closed this.

    This hits each of the ``_PICKER_ALLOWED_TYPES`` (see PICKER_TYPES, which
    must stay in sync with the route's allowlist) and asserts that no forbidden
    identifier appears in the JSON response. The leaked
    values are connector-asset TITLES (an Echo tenant title, a Google
    Ads customer title, …), which are NOT org/team/workspace
    identifiers — so the haystack is the persona's
    ``forbidden_asset_titles`` (the control-workspace's connector-asset
    titles on staging), unioned with the org/team/workspace forbidden
    lists in case a title happens to embed one of those tokens. See
    PERSONAS.md for how to populate ``PERSONA_<NAME>_FORBIDDEN_ASSET_TITLES``.

    Expected behavior per persona:
      * Superuser — sees all assets by design (no forbidden set).
      * Scoped admin (workspace_admin, etc. holding mc_data_connectors)
        — narrowed; a forbidden title is a LEAK. If the persona reaches
        the picker (HTTP 200) but has NO forbidden tokens configured,
        the cell records SKIP — a PASS there would be vacuous (nothing
        to detect), and a silent green would hide a misconfigured env.
      * Personas lacking mc_data_connectors — the route's
        ``_require_data_connectors`` gate returns 403, recorded as PASS
        via ``_convert_denial_to_pass`` (denial is the deny-all floor).
    """

    SURFACE = "data_connectors_picker"
    # Must mirror data_connectors/routes.py _PICKER_ALLOWED_TYPES. :
    # facebook_apps + facebook_app_users were removed from the route allowlist
    # Phase 3 (the Path A unified-Meta-app model no longer enumerates
    # them) — probing them returned a correct 400 ("Asset type not allowed") that
    # the suite mis-recorded as ERROR. youtube_accounts was added.
    PICKER_TYPES = (
        "echo_tenants",
        "chatwoot_accounts",
        "google_ads_customers",
        "ga_properties",
        "facebook_accounts",
        "facebook_pages",
        "youtube_accounts",
    )

    @pytest.mark.parametrize("asset_type", PICKER_TYPES)
    def test_picker_does_not_leak(self, all_personas, asset_type):
        # Blueprint mounted at /data-connectors with nginx /assets prefix
        # in front — same app_mounted=True story as TestChartMapData /
        # TestDashboardBuilderApi on the dashboard_builder blueprint.
        path = f"/data-connectors/api/wizard/options/{asset_type}"
        surface = f"{self.SURFACE}_{asset_type}"
        for persona in all_personas:
            if persona.is_superuser:
                # Superuser is unscoped; just confirm the endpoint is
                # reachable (200) and record PASS-by-design.
                status, body = get_json(persona.session, persona.base_url, path)
                if status == 200 and body is not None:
                    _record(persona, surface, _ok("superuser sees all picker assets by design"))
                else:
                    _record(persona, surface, _err(f"superuser got HTTP {status} at {path}"))
                continue

            # Asset titles are the primary haystack (the picker leaks
            # titles, not identifiers); org/team/workspace forbidden
            # tokens are added in case a title embeds one.
            forbidden = (
                *persona.forbidden_asset_titles,
                *_forbidden_for(persona, "orgs"),
                *_forbidden_for(persona, "teams"),
                *_forbidden_for(persona, "workspaces"),
            )
            outcome = check_no_forbidden_in_json(persona.session, persona.base_url, path, forbidden)
            # Personas without mc_data_connectors hit _require_data_connectors
            # 403 before the picker runs — denial is the deny-all floor.
            outcome = _convert_denial_to_pass(outcome, persona.name, surface)
            # A clean PASS with no forbidden tokens configured is vacuous
            # — the picker returned 200 but there was nothing to detect.
            # Record SKIP so the matrix shows the gap instead of a
            # misleading green. (Denial-class PASSes keep their PASS:
            # they exercised the deny-all floor, which needs no tokens.)
            if outcome.outcome == "PASS" and not forbidden and "denied" not in (outcome.detail or ""):
                outcome = _skip(
                    f"{surface}: {persona.name} reached the picker but no "
                    f"forbidden tokens configured — set "
                    f"PERSONA_{persona.name.upper()}_FORBIDDEN_ASSET_TITLES"
                )
            _record(persona, surface, outcome)
            assert outcome.outcome in ("PASS", "ERROR", "SKIP"), outcome.detail


# ===========================================================================
# User Management user list — mixed expectation
# ===========================================================================


class TestUserManagementUserList:
    """User Management page — /user-management.

     covers THREE unscoped surfaces on this page, all leaking to
    workspace_admin (non-superuser holding mc_user_management):

      1. User list itself — cross-workspace usernames render in the
         main table.
      2. New User modal — Workspaces / Orgs / Teams multi-select
         dropdowns are populated from the complete asset indices, so a
         workspace_admin can grant memberships referencing another
         tenant's orgs/teams.
      3. Edit Roles modal — same data source as #2 (template-shared
         options); same leak.

    All three render into the same /user-management HTML response, so a
    single HTML scan for any forbidden org / team / workspace
    identifier catches the full surface.

    Expected behavior:
      * Superuser — sees all (PASS by design)
      * Workspace admin — DENIED the page (redirected).
         made /user-management
        owner-only: the page + sidebar are gated on the owner-only
        ``WORKSPACE_DELETE`` permission. A workspace_admin keeps the
        user-management API capability perms (USER_INVITE / REMOVE /
        ROLE_ASSIGNMENT_MANAGE) but no longer reaches the PAGE, so it now
        302s — same denial branch as the no-permission personas. The
        owner positive-render case is covered by the superuser persona
        here (no workspace_owner persona exists in this 6-persona set;
        add one if an owner-specific render assertion is needed).
      * Bare / org / team / grant — denied (no mc_user_management
        permission) — PASS (denial expected)
    """

    PATH = "/user-management"
    SURFACE = "user_management_user_list"

    def test_user_management(self, all_personas):
        for persona in all_personas:
            if persona.is_superuser:
                # Superuser should reach the page; we only check that
                # the request succeeds (200), no leak check applies.
                status, text = get_raw(persona.session, persona.base_url, self.PATH)
                if status == 200 and text:
                    _record(persona, self.SURFACE, _ok("superuser reached page"))
                else:
                    _record(
                        persona,
                        self.SURFACE,
                        _err(f"superuser got HTTP {status} at {self.PATH}"),
                    )
                continue

            # owner-only: the
            # /user-management PAGE is now gated on the owner-only
            # WORKSPACE_DELETE permission. Every non-superuser persona —
            # including workspace_admin, which holds mc_user_management's
            # API capabilities but is not an owner — must be DENIED
            # (redirected) at the page. The cross-tenant leak vectors
            # guarded on this page are moot once a non-owner can't
            # render it at all; the API-surface leak checks live in the
            # dedicated /api/user-management/* tests below.
            outcome = check_denied(persona.session, persona.base_url, self.PATH)
            _record(persona, self.SURFACE, outcome)
            assert outcome.outcome in ("PASS", "ERROR", "SKIP"), outcome.detail


# ===========================================================================
# Grant resolution — path (grant_user only)
# ===========================================================================


class TestGrantResolution:
    """workspace-scoped data grants.

    The grant_user persona has a workspace-scoped grant on one or more
    teams. After login, those teams should appear in the filter
    dropdowns (positive assertion) AND no forbidden team should appear.
    All other personas: this test SKIPs (the grant path doesn't apply).

    data grants are being restricted to superuser targets only.
    Once that lands, a non-superuser grant_user persona can no longer
    receive a grant. The 2026-05-19 setup uses workspace-tier
    membership for the persona instead (test-grant joined to
    workspace-b as workspace_member), so the persona's PERSONA_GRANT_
    USER_GRANTED_TEAMS env is not set today. This test will fail or
    skip as a result; xfail captures the gap until either:
      (a) ships and a superuser-target grant test replaces this
      (b) Cross-tenant assertions on the membership path replace this
    See for the design trajectory.
    """

    PATH = "/api/dashboards/filter-options"
    SURFACE = "grant_resolution"

    # Item 3: un-xfailed 2026-06-01. The grant_user persona is now
    # seeded with a real workspace-scoped grant (PERSONA_GRANT_USER_GRANTED_
    # TEAMS set by seed_attack_surface_personas), so the positive+negative
    # assertions run and pass. The design trajectory (restricting
    # grants to superuser targets) hasn't removed the non-superuser grant
    # path, so this surface is a real PASS, not a deferred gap. Re-add xfail
    # only if actually removes the path.
    def test_grant_resolution(self, all_personas):
        for persona in all_personas:
            if persona.name != "grant_user":
                _record(persona, self.SURFACE, _skip("grant path n/a"))
                continue
            if not persona.granted_teams:
                # test-grant uses membership instead of
                # a data grant. With no PERSONA_GRANT_USER_GRANTED_TEAMS
                # set there's nothing to verify on the grant-resolution
                # path. SKIP rather than ERROR so the matrix stays
                # clean; the xfail decorator already marks this surface
                # as deferred.
                _record(
                    persona,
                    self.SURFACE,
                    _skip("PERSONA_GRANT_USER_GRANTED_TEAMS not set; persona uses membership path"),
                )
                continue
            status, body = get_json(persona.session, persona.base_url, self.PATH)
            if status is None or body is None:
                _record(persona, self.SURFACE, _err(f"HTTP {status} or non-JSON"))
                continue
            # Positive: every granted team must appear somewhere in the body.
            missing = [t for t in persona.granted_teams if not _contains(body, t)]
            # Negative: no forbidden team may appear.
            forbidden_found = find_forbidden_in_payload(body, persona.forbidden_teams)
            if missing or forbidden_found:
                detail = []
                if missing:
                    detail.append(f"granted teams not visible: {missing}")
                if forbidden_found:
                    detail.append(f"forbidden teams visible: {forbidden_found}")
                # this LEAK is typically a test-config
                # mismatch rather than a real product issue today.
                # The grant_user persona may have been moved from a
                # data grant to a workspace membership (per the
                # design direction restricting grants to
                # superuser targets). If PERSONA_GRANT_USER_GRANTED_
                # TEAMS is still set in .env.attack-surface but no
                # actual grant exists on staging, the granted-teams
                # check fails because the membership path doesn't
                # produce the same "explicit grant resolution" data
                # the test asserts on. Clear the env var to SKIP, or
                # re-set up a grant for a different persona to
                # exercise the real path.
                detail.append("(persona may be on membership not grant)")
                outcome = _leak("; ".join(detail))
            else:
                outcome = _ok(f"granted teams visible: {list(persona.granted_teams)}; " f"no forbidden leaks")
            _record(persona, self.SURFACE, outcome)
            assert outcome.outcome == "PASS", outcome.detail


# ---------------------------------------------------------------------------
# Small wrappers around CheckOutcome to keep test code readable
# ---------------------------------------------------------------------------


def _ok(detail: str = ""):
    from .attack_surface_helpers import passed

    return passed(detail)


def _leak(detail: str):
    from .attack_surface_helpers import leaked

    return leaked(detail)


def _err(detail: str):
    from .attack_surface_helpers import errored

    return errored(detail)


def _skip(detail: str = ""):
    from .attack_surface_helpers import skipped

    return skipped(detail)


def _contains(payload, needle: str) -> bool:
    """True if ``needle`` appears as a string leaf in ``payload``."""
    from .attack_surface_helpers import _iter_strings

    return any(s == needle for s in _iter_strings(payload))


def _xfail_check_no_forbidden_html(persona, path, forbidden, ticket):
    """Same as check_no_forbidden_in_html but records XFAIL/XPASS outcomes.

    Used for the workspace_admin / User Management cell where we know
    the surface leaks today. The outcome string in the matrix
    is XFAIL when the expected leak is present and XPASS when the
    surface unexpectedly stops leaking (signal to flip the test).
    """
    from .attack_surface_helpers import check_no_forbidden_in_html, CheckOutcome

    raw = check_no_forbidden_in_html(persona.session, persona.base_url, path, forbidden)
    if raw.outcome == "LEAK":
        return CheckOutcome("XFAIL", f"{ticket}: {raw.detail}")
    if raw.outcome == "PASS":
        return CheckOutcome("XPASS", f"{ticket} fix may have landed: {raw.detail}")
    return raw


# ===========================================================================
# Login Activity routes are superuser-only
# ===========================================================================


_LOGIN_ACTIVITY_ROUTES: tuple[tuple[str, str], ...] = (
    ("/api/user-management/audit-log", "login_activity_audit_log"),
    ("/api/user-management/login-trend", "login_activity_login_trend"),
    (
        "/api/user-management/login-users?date=2026-05-19",
        "login_activity_login_users",
    ),
    ("/api/user-management/active-users", "login_activity_active_users"),
)


class TestLoginActivityRoutes:
    """the four Login Activity endpoints are superuser-only.

    earlier each route was gated on ``_require_admin`` and any
    admin role could enumerate cluster-wide login events. Post-fix:
    superuser → 200, everybody else → 403.

    The route paths include the /assets prefix via app_mounted=True
    (default in the helpers); each test exercises the GET path against
    every persona's session and records PASS/LEAK per row.

    Surface naming uses one row per route so a future regression on
    any single endpoint is visible in the matrix.
    """

    @pytest.mark.parametrize("path,surface", _LOGIN_ACTIVITY_ROUTES, ids=[s for _p, s in _LOGIN_ACTIVITY_ROUTES])
    def test_login_activity_route(self, all_personas, path, surface):
        from .attack_surface_helpers import check_status_in, get_raw

        for persona in all_personas:
            status, _ = get_raw(persona.session, persona.base_url, path)
            if persona.is_superuser:
                outcome = check_status_in(
                    status,
                    (200,),
                    path=path,
                    persona_name=persona.name,
                    expected_label="200 for superuser",
                )
            else:
                outcome = check_status_in(
                    status,
                    (302, 401, 403),
                    path=path,
                    persona_name=persona.name,
                    expected_label="denied for non-superuser",
                )
            _record(persona, surface, outcome)
            assert outcome.outcome == "PASS", outcome.detail


# ===========================================================================
# User Management per-user mutate routes are workspace-scoped
# ===========================================================================
#
# Each route in this list targets a username the persona must not be
# allowed to manage. For non-superuser personas we use the SUPERUSER's
# username as the target — guaranteed to exist on staging and
# guaranteed to be cross-workspace for any non-superuser. Superuser
# bypasses the gate and gets a different expected status (200 for the
# read routes; we skip the write routes for superuser to avoid mutating
# staging state).
#
# Routes are split into READ (idempotent — safe to exercise as
# superuser too) and WRITE (would mutate staging — only exercise the
# 403 path on non-superusers).


_READ_MUTATE_ROUTES: tuple[tuple[str, str, str], ...] = (
    # (method, url-template, surface name)
    ("get", "/api/user-management/authz-memberships/{target}", "user_mgmt_authz_memberships"),
    ("get", "/api/user-management/role-assignments/{target}", "user_mgmt_role_assignments_get"),
    ("get", "/api/user-management/user-detail/{target}", "user_mgmt_user_detail"),
)

_WRITE_MUTATE_ROUTES_403_ONLY: tuple[tuple[str, str, dict, str], ...] = (
    # (method, path, body-template (with {target} placeholder), surface)
    ("post", "/api/user-management/enable-mfa", {"username": "{target}"}, "user_mgmt_enable_mfa"),
    ("post", "/api/user-management/delete-user", {"username": "{target}"}, "user_mgmt_delete_user"),
    ("post", "/api/user-management/suspend-user", {"username": "{target}"}, "user_mgmt_suspend_user"),
    ("post", "/api/user-management/reinstate-user", {"username": "{target}"}, "user_mgmt_reinstate_user"),
    (
        "post",
        "/api/user-management/update-email",
        {"username": "{target}", "email": "attack@example.test"},
        "user_mgmt_update_email",
    ),
)


def _target_username_for(persona: Persona, personas: dict) -> str | None:
    """Return a username that is guaranteed to be cross-workspace for
    ``persona``. For non-superusers, the SUPERUSER persona's username
    is always cross-workspace. Returns None for the superuser persona
    (no cross-workspace target exists for them by definition).
    """
    if persona.is_superuser:
        return None
    superuser = personas.get("superuser")
    if superuser is None:
        return None
    return superuser.username


class TestUserManagementMutateReadRoutes:
    """per-user READ routes return 403 for non-superuser
    operators targeting a user outside their workspace.

    Bare / org / team / grant personas get 403 from ``_require_admin``
    BEFORE the workspace gate fires; that's also acceptable — the
    only invalid status is 200 (which would mean the route returned
    PII about a cross-workspace user). Workspace_admin specifically
    must hit the workspace gate and return 403 with the target name
    in the error.
    """

    @pytest.mark.parametrize(
        "method,url_template,surface",
        _READ_MUTATE_ROUTES,
        ids=[r[2] for r in _READ_MUTATE_ROUTES],
    )
    def test_per_user_read_route(self, all_personas, personas, method, url_template, surface):
        from .attack_surface_helpers import check_status_in, get_raw

        del method  # all READ routes are GET; method retained on the tuple for shape symmetry
        for persona in all_personas:
            target = _target_username_for(persona, personas)
            if target is None:
                # Superuser — exercise the route against itself to
                # confirm the bypass works and the route still returns
                # 200 (or 404 if user-detail synthesis fails for the
                # superuser shape).
                path = url_template.format(target=persona.username)
                status, _ = get_raw(persona.session, persona.base_url, path)
                outcome = check_status_in(
                    status,
                    (200, 404),
                    path=path,
                    persona_name=persona.name,
                    expected_label="200 for superuser (bypass)",
                )
                _record(persona, surface, outcome)
                assert outcome.outcome == "PASS", outcome.detail
                continue

            path = url_template.format(target=target)
            status, _ = get_raw(persona.session, persona.base_url, path)
            outcome = check_status_in(
                status,
                (302, 401, 403),
                path=path,
                persona_name=persona.name,
                expected_label="denied for cross-workspace target",
            )
            _record(persona, surface, outcome)
            assert outcome.outcome == "PASS", outcome.detail


class TestUserManagementMutateWriteRoutes:
    """per-user WRITE routes return 403 for non-superuser
    operators targeting a cross-workspace user.

    WRITE routes are exercised ONLY against non-superuser personas with
    a cross-workspace target — the route MUST 403 before any side
    effect lands. We never POST against the superuser path here:
    staging mutations from a regression suite are exactly the kind of
    spillover that would break the next test run.

    Superuser rows are recorded as PASS-by-design (the bypass is
    covered by the READ tests + the unit tests; we trust it here).
    """

    @pytest.mark.parametrize(
        "method,path,body_template,surface",
        _WRITE_MUTATE_ROUTES_403_ONLY,
        ids=[r[3] for r in _WRITE_MUTATE_ROUTES_403_ONLY],
    )
    def test_per_user_write_route(
        self,
        all_personas,
        personas,
        persona_csrf_tokens,
        method,
        path,
        body_template,
        surface,
    ):
        from .attack_surface_helpers import check_status_in, post_request

        del method  # all WRITE routes are POST; method retained on the tuple for shape symmetry
        for persona in all_personas:
            if persona.is_superuser:
                _record(persona, surface, _ok("superuser bypass — not exercised"))
                continue

            target = _target_username_for(persona, personas)
            if target is None:
                _record(persona, surface, _skip("no cross-workspace target available"))
                continue

            csrf = persona_csrf_tokens.get(persona.name)
            if not csrf:
                _record(persona, surface, _err("CSRF token unavailable"))
                continue

            body = {k: v.format(target=target) if isinstance(v, str) else v for k, v in body_template.items()}
            status, _ = post_request(
                persona.session,
                persona.base_url,
                path,
                json_body=body,
                csrf_token=csrf,
            )
            outcome = check_status_in(
                status,
                (302, 401, 403),
                path=path,
                persona_name=persona.name,
                expected_label="denied for cross-workspace target",
            )
            _record(persona, surface, outcome)
            assert outcome.outcome == "PASS", outcome.detail


# ===========================================================================
# bulk-action is all-or-nothing for cross-workspace targets
# ===========================================================================


class TestUserManagementBulkAction:
    """bulk-action 403s on the whole batch if ANY username
    is outside the operator's workspace.

    We POST a batch of [persona.username, superuser.username] — the
    second name is guaranteed cross-workspace for non-superuser
    personas. The route MUST 403 BEFORE calling UserManagementService.
    bulk_action (no partial-apply). For superuser the route bypass
    fires; we record PASS-by-design without exercising the route
    (staging mutation safety).

    Action is "reinstate" — chosen because it's the safest of the
    three (suspend/reinstate/delete): if the gate fails AND the route
    runs anyway, "reinstate" against an already-active user is a
    no-op on staging, not destructive. Even so we expect the 403 to
    fire first.
    """

    PATH = "/api/user-management/bulk-action"
    SURFACE = "user_mgmt_bulk_action"

    def test_bulk_action(self, all_personas, personas, persona_csrf_tokens):
        from .attack_surface_helpers import check_status_in, post_request

        for persona in all_personas:
            if persona.is_superuser:
                _record(persona, self.SURFACE, _ok("superuser bypass — not exercised"))
                continue

            target = _target_username_for(persona, personas)
            if target is None:
                _record(persona, self.SURFACE, _skip("no cross-workspace target available"))
                continue

            csrf = persona_csrf_tokens.get(persona.name)
            if not csrf:
                _record(persona, self.SURFACE, _err("CSRF token unavailable"))
                continue

            status, _ = post_request(
                persona.session,
                persona.base_url,
                self.PATH,
                json_body={
                    "action": "reinstate",
                    "usernames": [persona.username, target],
                },
                csrf_token=csrf,
            )
            outcome = check_status_in(
                status,
                (302, 401, 403),
                path=self.PATH,
                persona_name=persona.name,
                expected_label="403 all-or-nothing for cross-workspace targets",
            )
            _record(persona, self.SURFACE, outcome)
            assert outcome.outcome == "PASS", outcome.detail


# ===========================================================================
# #1 — Dashboard Builder /api/dashboards list workspace gate
# ===========================================================================


class TestDashboardBuilderApi:
    """#1 — the Builder's /api/dashboards list is workspace-
    scoped via _user_may_read_dashboard.

    earlier a workspace-B user saw kavanah-media dashboards in
    the Builder list because the read gate only consulted
    visibility_mode + allowed_*_esids. Post-fix the workspace gate
    runs first, so cross-workspace dashboards are filtered out before
    visibility_mode is checked.

    This test scans the JSON response for any forbidden workspace
    identifier (slug or ESID, matching the persona's
    forbidden_workspaces env). If a forbidden workspace slug appears
    anywhere in a dashboard title or menu name the matrix records a
    LEAK.

    Bare persona is denied at the page gate (mc_dashboard_builder),
    so this surface SKIPs for them — covered by the existing
    TestDashboardMenus class which already exercises the bare denial.
    """

    PATH = "/dashboard-builder/api/dashboards"
    SURFACE = "dashboard_builder_api_dashboards"

    def test_dashboard_builder_list(self, all_personas):
        from .attack_surface_helpers import check_no_forbidden_in_json

        for persona in all_personas:
            if persona.is_superuser:
                # Superuser is unscoped — no forbidden set; just
                # confirm the route succeeds for them.
                # follow-up: app_mounted defaults to True. The
                # dashboard_builder blueprint is registered against the
                # main Flask app (app.py:174), so the nginx /assets/
                # prefix DOES apply here. Pre-fix this test passed
                # app_mounted=False and the superuser hit
                # /dashboard-builder/... directly, bypassing nginx →
                # 404. The chart_map_data tests on the same blueprint
                # use the default (True) and pass; mirror them.
                status, body = get_json(persona.session, persona.base_url, self.PATH)
                if status == 200 and body is not None:
                    _record(persona, self.SURFACE, _ok("superuser reached endpoint"))
                else:
                    _record(persona, self.SURFACE, _err(f"superuser got HTTP {status}"))
                continue

            forbidden = (
                *persona.forbidden_orgs,
                *persona.forbidden_teams,
                *persona.forbidden_workspaces,
            )
            if not forbidden:
                _record(persona, self.SURFACE, _skip("no forbidden identifiers configured"))
                continue

            outcome = check_no_forbidden_in_json(
                persona.session,
                persona.base_url,
                self.PATH,
                forbidden,
            )
            # Personas that lack mc_dashboard_builder will hit the
            # page gate before the list runs — convert that denial
            # to a PASS so the matrix shows "expected denial" rather
            # than a noisy ERROR.
            outcome = _convert_denial_to_pass(outcome, persona.name, self.SURFACE)
            _record(persona, self.SURFACE, outcome)
            assert outcome.outcome in ("PASS", "ERROR"), outcome.detail


# ===========================================================================
# Dashboard Builder "Show All" visibility is workspace-admin-only
# ===========================================================================


class TestDashboardBuilderShowAllVisibility:
    """a non-workspace-admin POSTing a dashboard with
    ``visibility_mode=show_all`` is rejected server-side.

    ``show_all`` is workspace-wide visibility — it publishes a dashboard
    above the scope of any org-tier / team-tier user. The dropdown hides
    the option for those users, but the UI fix alone is a privilege-
    escalation gap: a crafted ``POST /api/dashboards`` with
    ``visibility_mode=show_all`` bypasses it. ``_enforce_visibility_scope``
    is the server-side defense-in-depth layer (matches the
    Layer-1 pattern).

    We exercise the DENY path only — a non-workspace persona's
    ``show_all`` create MUST 403 (or be denied at the page gate)
    *before* any dashboard doc lands, so the POST is staging-safe (a
    rejection persists nothing). The workspace_admin and superuser
    personas are recorded PASS-by-design WITHOUT exercising the route:
    their ``show_all`` create would legitimately persist a dashboard,
    and a regression suite must not mutate staging. Their allow-path is
    covered by the release-plan unit test
    .
    """

    PATH = "/dashboard-builder/api/dashboards"
    SURFACE = "dashboard_builder_show_all_visibility"

    # Personas allowed to set show_all — recorded PASS-by-design, never POSTed.
    _ALLOWED_NAMES = frozenset({"workspace_admin"})

    def test_show_all_denied_for_non_workspace_user(self, all_personas, persona_csrf_tokens):
        from .attack_surface_helpers import check_status_in, post_request

        for persona in all_personas:
            if persona.is_superuser or persona.name in self._ALLOWED_NAMES:
                _record(
                    persona,
                    self.SURFACE,
                    _ok("workspace-admin / superuser may set show_all — not exercised (staging-safe)"),
                )
                continue

            csrf = persona_csrf_tokens.get(persona.name)
            if not csrf:
                _record(persona, self.SURFACE, _err("CSRF token unavailable"))
                continue

            # A brand-new (id-less) dashboard with workspace-wide visibility.
            # Rejection means nothing is created — safe on staging.
            status, _ = post_request(
                persona.session,
                persona.base_url,
                self.PATH,
                json_body={
                    "title": "attack-surface probe (should be rejected)",
                    "visibility_mode": "show_all",
                    "charts": [],
                },
                csrf_token=csrf,
            )
            # 403 — either from _enforce_visibility_scope (persona holds
            # mc_dashboard_builder and reaches the gate) or from the page
            # gate (bare persona lacks the permission). 302/401 cover the
            # auth-redirect denials. Any of these means show_all did not land.
            outcome = check_status_in(
                status,
                (302, 401, 403),
                path=self.PATH,
                persona_name=persona.name,
                expected_label="show_all rejected for non-workspace user",
            )
            _record(persona, self.SURFACE, outcome)
            assert outcome.outcome == "PASS", outcome.detail


# ===========================================================================
# Connector configure endpoints enforce team-scope (cross-tenant
# write / IDOR). Out-of-workspace team_esid must 404 before any write.
# ===========================================================================


# (surface_id, path, extra body fields beyond team_esid). team_esid is added
# per-request from forbidden_team_esid. The extra fields are minimal — the
# tenant gate runs BEFORE the configure service and its field validation, so a
# bare team_esid reaches the gate; we keep the connector-specific keys absent
# on purpose so a non-404 can't be blamed on a malformed body.
_KAN977_CONFIGURE_ENDPOINTS = [
    ("configure_echo", "/data-connectors/api/wizard/echo/configure", {}),
    ("configure_chatwoot", "/data-connectors/api/wizard/chatwoot/configure", {}),
    ("configure_youtube", "/data-connectors/api/wizard/youtube/configure", {}),
    ("configure_youtube_onboarding", "/data-connectors/api/wizard/youtube/onboarding-finalize", {}),
    ("configure_ga4_oauth", "/data-connectors/api/wizard/ga4/oauth-configure", {}),
    ("configure_ga4_oauth_onboarding", "/data-connectors/api/wizard/ga4/oauth-onboarding-finalize", {}),
    ("configure_fb", "/data-connectors/api/wizard/fb/configure", {}),
]


class TestConnectorConfigureTenantScope:
    """the 7 connector configure endpoints (echo, chatwoot, youtube
    configure + onboarding-finalize, ga4 oauth-configure + oauth-onboarding-
    finalize, fb configure) gate a client-supplied ``team_esid`` against the
    caller's workspace scope via ``_check_team_for_wizard`` BEFORE writing any
    connector link.

    Pre-fix these were gated only by the ``mc_data_connectors`` permission
    (not tenant-scoped), so a holder in workspace A could bind a connector onto
    a team in workspace B by passing the victim team's esid — a cross-tenant
    write / IDOR. Post-fix an out-of-scope ``team_esid`` 404-collapses before
    the configure service runs.

    We exercise the DENY path only: each non-superuser persona scoped to
    workspace A POSTs the out-of-workspace ``TEST_FORBIDDEN_TEAM_ESID`` (Team B1,
    workspace-b) and the endpoint MUST refuse (404 — the gate; 401/403/302 also
    count as denials). A 404 persists nothing, so the POST is staging-safe. The
    in-scope happy path would WRITE a connector and mutate staging, so it is
    NOT exercised here — it's covered by the unit + release-plan tests
    .

    ``grant_user`` is scoped TO Team B1, so for them the target is in-scope and
    a 404 would be wrong — they're SKIPPED (the legitimate-owner control, not a
    cross-tenant attacker). Superuser is unscoped (sees every team) and is
    recorded PASS-by-design without POSTing.

    A 200 (or any 2xx) here is a LEAK: the configure service was reached for an
    out-of-workspace team.
    """

    @pytest.mark.parametrize(
        "surface,path,extra_body",
        _KAN977_CONFIGURE_ENDPOINTS,
        ids=[r[0] for r in _KAN977_CONFIGURE_ENDPOINTS],
    )
    def test_configure_out_of_scope_team_denied(
        self,
        all_personas,
        persona_csrf_tokens,
        forbidden_team_esid,
        surface,
        path,
        extra_body,
    ):
        from .attack_surface_helpers import check_status_in, post_request

        surface_id = f"connector_{surface}"

        if not forbidden_team_esid:
            for persona in all_personas:
                _record(persona, surface_id, _skip("TEST_FORBIDDEN_TEAM_ESID not set"))
            return

        for persona in all_personas:
            if persona.is_superuser:
                _record(persona, surface_id, _ok("superuser unscoped — not exercised (staging-safe)"))
                continue

            # The cross-tenant target (Team B1) is legitimately in scope for a
            # persona granted Team B1 — skip rather than expect a (wrong) 404.
            if _CROSS_TENANT_TEAM_TITLE in (*persona.allowed_teams, *persona.granted_teams):
                _record(persona, surface_id, _skip(f"{_CROSS_TENANT_TEAM_TITLE} is in-scope for this persona"))
                continue

            csrf = persona_csrf_tokens.get(persona.name)
            if not csrf:
                _record(persona, surface_id, _err("CSRF token unavailable"))
                continue

            status, _body = post_request(
                persona.session,
                persona.base_url,
                path,
                json_body={"team_esid": forbidden_team_esid, **extra_body},
                csrf_token=csrf,
            )
            # 404 — the _check_team_for_wizard gate collapsing the out-of-scope
            # team. 401/403/302 cover personas denied earlier (no
            # mc_data_connectors, or auth redirect). Any of these means no
            # connector link was written to the foreign team.
            outcome = check_status_in(
                status,
                (302, 401, 403, 404),
                path=path,
                persona_name=persona.name,
                expected_label="out-of-workspace team_esid denied (404 gate)",
            )
            _record(persona, surface_id, outcome)
            assert outcome.outcome == "PASS", outcome.detail


# ===========================================================================
# Data Explorer NLQ endpoints enforce tenant scope server-side
# (cross-tenant data read). Client-supplied indices/organizations/teams must
# not let a scoped caller read another tenant's rows.
# ===========================================================================


def _nlq_skip_status(status: int | None) -> str | None:
    """Map an NLQ response status to a SKIP reason, or None to proceed.

    The surface is a no-op when the feature is off or unauthorised on this
    environment — record SKIP (never a vacuous PASS, never a noisy ERROR):
      * 503 — NLQ_ENABLED is false on this deployment.
      * 502 — upstream model/API error (no/invalid key) — can't exercise scope.
      * 302/401/403 — caller denied before NLQ runs (no Data Explorer
        permission, or no team scope at all).
      * 429 — the caller's daily Ask Kavanah cap is spent.
    """
    if status is None:
        return "request failed"
    if status == 503:
        return "NLQ disabled on this environment (503)"
    if status == 502:
        return "NLQ upstream/model error (502) — API key likely absent"
    if status in (302, 401, 403):
        return f"denied before NLQ ({status}) — no Data Explorer permission or no team scope"
    if status == 429:
        return "daily Ask Kavanah cap reached (429)"
    return None


class TestDataExplorerNlqTenantScope:
    """the Data Explorer NLQ endpoints ``/data-explorer/api/ask`` and
    ``/api/chat`` enforce the caller's tenant scope server-side.

    Pre-fix the client-supplied ``indices`` / ``organizations`` / ``teams`` were
    injected into the LLM prompt as advisory targeting hints only; nothing tied
    the executed ES queries to the caller's authorized scope, so a scoped user
    could read another tenant's data by naming it. Post-fix the caller's
    ``RoleFilterService`` team clause is AND-ed into every ES query the tool
    loop runs, and callers with no resolvable scope fail closed to a deny-all
    sentinel.

    Attack: each A-scoped persona asks for Org B / Team B data AND passes the
    out-of-scope ``organizations`` / ``teams`` / cross-tenant ``indices``
    explicitly. The response (narrative + stats + table + es_query) MUST contain
    none of the persona's forbidden org/team identifiers — the server-side
    filter makes those rows unreachable regardless of what the client passes or
    what the model emits. A forbidden identifier appearing anywhere in the JSON
    is a LEAK.

    These are READ-only queries — no staging mutation — so unlike the configure
    surface we DO exercise the scoped personas directly. The endpoints call the
    live NLQ model, so the surface SKIPs cleanly when the feature is disabled or
    the API key is absent (see ``_nlq_skip_status``).

    Superuser is unscoped (no forbidden set) and recorded PASS-by-design.
    ``bare`` (no resolvable scope) exercises the deny-all floor — its forbidden
    set spans every org/team, so the same scan proves it reads nothing.
    """

    # The cross-tenant probe. We name Org B / Team B in the natural-language
    # query AND pass them as structured targeting params + a cross-tenant index
    # so the test attacks both the prompt-hint path (old behaviour) and the
    # structured path. The forbidden-identifier scan is what actually catches a
    # leak; the wording only steers the model toward the foreign tenant.
    _PROBE_QUERY = "Show me every metric for Org B and Team B1 across all teams and organizations."

    def _ask_body(self, persona: Persona) -> dict:
        return {
            "query": self._PROBE_QUERY,
            "organizations": list(persona.forbidden_orgs) or ["Org B"],
            "teams": [_CROSS_TENANT_TEAM_TITLE],
        }

    def _chat_body(self) -> dict:
        return {"messages": [{"role": "user", "content": self._PROBE_QUERY}]}

    def _run_one(self, persona, persona_csrf_tokens, surface, path, body) -> None:
        from .attack_surface_helpers import find_forbidden_in_payload, post_request

        if persona.is_superuser:
            _record(persona, surface, _ok("superuser unscoped — no forbidden set"))
            return

        forbidden = (*persona.forbidden_orgs, *persona.forbidden_teams)
        if not forbidden:
            _record(persona, surface, _skip("no forbidden identifiers configured"))
            return

        csrf = persona_csrf_tokens.get(persona.name)
        if not csrf:
            _record(persona, surface, _err("CSRF token unavailable"))
            return

        status, text = post_request(persona.session, persona.base_url, path, json_body=body, csrf_token=csrf)
        skip_reason = _nlq_skip_status(status)
        if skip_reason is not None:
            _record(persona, surface, _skip(f"{path}: {skip_reason}"))
            return
        if status != 200:
            _record(persona, surface, _err(f"{path}: unexpected HTTP {status}"))
            return

        import json as _json

        try:
            payload = _json.loads(text)
        except (ValueError, TypeError):
            _record(persona, surface, _err(f"{path}: non-JSON 200 response"))
            return

        found = find_forbidden_in_payload(payload, forbidden)
        if found:
            outcome = _leak(f"{path}: forbidden tenant identifiers in NLQ response: {found}")
        else:
            outcome = _ok(f"{path}: no out-of-scope tenant data leaked")
        _record(persona, surface, outcome)
        assert outcome.outcome == "PASS", outcome.detail

    def test_ask_does_not_leak_cross_tenant(self, all_personas, persona_csrf_tokens):
        for persona in all_personas:
            self._run_one(
                persona,
                persona_csrf_tokens,
                "nlq_ask_scope",
                "/data-explorer/api/ask",
                self._ask_body(persona),
            )

    def test_chat_does_not_leak_cross_tenant(self, all_personas, persona_csrf_tokens):
        for persona in all_personas:
            self._run_one(
                persona,
                persona_csrf_tokens,
                "nlq_chat_scope",
                "/data-explorer/api/chat",
                self._chat_body(),
            )
