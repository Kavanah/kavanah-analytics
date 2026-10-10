# Attack-surface personas — setup

One-time setup for `tests/security/test_attack_surface.py`. Each persona is a
real account on the target environment; the suite logs each one in via scripted
POST to `/login`. Second factor is satisfied by a shared TOTP seed
(`PERSONA_SHARED_MFA_SECRET`) so the real `/mfa/challenge` flow is exercised
; the break-glass bypass remains as a fallback when the seed is
absent.

**Two halves, two tools:**

1. **Accounts (automated):** `bin/python/tools/seed_attack_surface_personas.py`
   creates the 10 persona accounts, sets known passwords, stamps the shared MFA
   seed, writes the scope docs, and emits `.env.attack-surface`. Runs
   in-container against ES — **no admin login, no superuser credential.**
2. **Structures (manual, one-time):** the workspaces / orgs / teams the
   personas are scoped *to*, plus two tripwire assets. Created by hand in the
   Asset Explorer UI (see "Manual structure setup" below). The seeder resolves
   these by title/slug and **fails loudly** if one is missing — so do this part
   first.

## Personas (10)

| Name               | Shape                                                       | Scoped to            |
| ------------------ | ----------------------------------------------------------- | -------------------- |
| `bare`             | no system role, no scope, no memberships                    | —                    |
| `workspace_admin`  | no system role; workspace_admin scope role                  | workspace-a          |
| `org_user`         | no system role; org-tier scope role                         | Org A                |
| `team_user`        | no system role; team-tier scope role                        | Team A1 (under Org A)|
| `grant_user`       | no system role; workspace-scoped data grant                 | Team B1 (workspace B)|
| `superuser`        | superuser (sees everything by design)                       | all                  |
| `team_grant` ★     | `de_authz_user_team_role` row **only, NO membership doc**   | Team A1              |
| `workspace_member`★| `workspace_member` role_esid **only, NO membership doc**    | workspace-a          |
| `org_grant` ★      | `de_authz_user_org_role` row **only, NO membership doc**    | Org A                |
| `workspace_grant`★ | workspace_admin role_esid **only, NO membership doc**       | workspace-a          |

**System roles:** only the `superuser` persona carries one (`superuser`). Every
other persona is created with **no ES system role** — their access comes
entirely from the `de_authz_user_*_role` scope assignment docs, exactly like a
real scoped viewer.

★ = grant-only cohort — the shapes that broke prod beta-testers.
Post-** (P3b)** membership docs are retired entirely: *every* persona is
now a single `de_authz_user_*_role` role doc (the role doc carries `is_primary`;
readers resolve "belongs to" from it). So the historical membership-vs-grant
distinction no longer changes what's written — the ★ label is kept only because
these personas are the named positive controls for the resolver's non-privileged
branch. The seeder writes all personas as a single role doc, matching
`app.py:_provision_new_authz_assignments`.

## Manual structure setup (one-time, per environment)

Create these in the Asset Explorer UI **before** running the seeder. ~12
"Add" form submissions. They rarely change once created.

**Workspaces** (Asset Explorer → Workspaces → Add):
- `workspace-a` — slug `workspace-a`
- `workspace-b` — slug `workspace-b`
- `control-workspace` — slug `control-workspace` (no persona is scoped here)

**Organizations** (→ Organizations → Add):
- `Org A` in workspace-a
- `Org B` in workspace-b
- `Control Org` in control-workspace

**Teams** (→ Teams → Add):
- `Team A1`, `Team A2` under Org A
- `Team B1`, `Team B2` under Org B

**Tripwire 1 — Data Connectors picker** (→ Echo Tenants → Add):
- Title **exactly** `ControlOrg Echo Tenant`, in **control-workspace**
- API Key: any throwaway (e.g. `attack-surface-tripwire`); Subdomain: `controlorg-tripwire`
- Must carry the control workspace's `workspace_esid`. A new asset is stamped
  with the **creator's *primary* workspace** — so create it as a
  control-workspace member, or as a superuser using the add form's
  superuser-only workspace field to target control-workspace explicitly. (Full
  detail + the ES verification query are in the "Data Connectors picker
  surface" section below.)

**Tripwire 2 — same-title collision** (→ Teams → Add):
- A team titled **exactly** `Acme` in **control-workspace**, under a
  control-workspace org also titled `Acme`. This duplicates a title a scoped
  persona can legitimately see, proving cross-workspace isolation by count.
  (Detail + verification query in the "Same-title collision" section
  below.) If you wire this to `team_user`, set its `SAME_TITLE_TEAM` env var.

## Account setup (automated — run the seeder)

From inside `imports_container`, after the structures exist:

```bash
# Dry run first (default — writes nothing, prints what it WOULD do):
docker compose exec imports_container python3 -m tools.seed_attack_surface_personas \
    --base-url https://<staging-host>

# Apply:
docker compose exec imports_container python3 -m tools.seed_attack_surface_personas \
    --base-url https://<staging-host> --apply
```

The seeder is **idempotent**: existing accounts are skipped. Pass
`--reset-passwords` to rotate the password of existing accounts and rewrite
their `.env` line (use when `.env.attack-surface` is lost or out of sync). It
writes `.env.attack-surface` (mode 600) with usernames, passwords, the shared
MFA seed, and the `FORBIDDEN_*`/`ALLOWED_*` values. Verify a persona's scope by
logging in manually once.

## `.env.attack-surface` — generated by the seeder, not hand-written

**Do not hand-write this file.** The seeder
(`bin/python/tools/seed_attack_surface_personas.py`) generates it on `--apply`:
usernames, generated passwords, the shared MFA seed, and every
`FORBIDDEN_*`/`ALLOWED_*` value derived from the persona matrix. The legacy
`.env.attack-surface.example` is kept only as a field reference; the seeder is
the source of truth.

### Operational gotchas (learned standing this up on staging, 2026-06-01)

These bit hard during the first staging run. Read before running on a new env.

1. **`/app` is container-only — `docker cp` the `.env` out.** The repo is
   bind-mounted into `imports_container` at `/app/python`, `/app/deployment`,
   `/app/tests` etc., but **`/app` itself is NOT mounted** (it's baked into the
   image). So `--env-file /app/.env.attack-surface` writes a file the host
   can't see, and it vanishes on `--force-recreate`. After `--apply`, copy it
   to the host where the runner looks:
   ```bash
   docker cp imports_container:/app/.env.attack-surface ~/DE-Reporting/.env.attack-surface
   chmod 600 ~/DE-Reporting/.env.attack-surface
   ```

2. **Exec with the right PYTHONPATH + workdir.** The mount is `/app/python`
   (from `bin/python`), so:
   ```bash
   docker compose -f deployment/docker-compose.yml exec \
     -e PYTHONPATH=/app/python:/app/python/asset_management/web \
     -w /app/python \
     imports_container python3 -m tools.seed_attack_surface_personas ...
   ```

3. **Persona usernames are emails AND the email field must match.** The web
   login treats any `@`-credential as an email and resolves it to a username
   (`find_user_by_email`) BEFORE checking the password. An `@`-form username
   with an empty email field fails with "invalid username or password" — the
   password is never checked. The seeder sets `email=username` so the resolver
   maps it back. (ES-layer auth works either way; only the web form needs it.)

4. **The `.env` is `source`d under `set -u` — values must be shell-safe.** The
   seeder single-quotes every value and avoids shell-active chars (`$` `` ` ``
   `\` `"` `!`) in generated passwords. Values with spaces/commas
   (`Control Org,Org B`) only work because they're quoted. After any `--apply`,
   sanity-check:
   ```bash
   ( set -u; source ~/DE-Reporting/.env.attack-surface && echo "SOURCE OK: $TEST_BASE_URL" )
   ```
   `$N: unbound variable` or `command not found` means a value isn't quoted.

5. **Asset-explorer add-form stamps the creator's primary workspace, not the
   selected one.** When creating the control-workspace tripwires (Acme org,
   Echo tenant), the add-form workspace selector is ignored and the asset lands
   in your primary workspace. Verify `workspace_esid` after creating, and
   re-stamp directly if wrong:
   ```bash
   # re-stamp an asset's workspace_esid (example: the Acme org):
   docker compose -f deployment/docker-compose.yml exec \
     -e PYTHONPATH=/app/python:/app/python/asset_management/web -w /app/python \
     imports_container python3 -c "
   from lib.es_helpers import get_elasticsearch_client
   es=get_elasticsearch_client()
   h=es.search(index='de_asset_organization',body={'query':{'match':{'organization_title':'Acme'}},'size':10})['hits']['hits']
   h=[x for x in h if x['_source'].get('organization_title')=='Acme']
   assert len(h)==1; es.update(index='de_asset_organization',id=h[0]['_id'],doc={'workspace_esid':'control-workspace'}); es.indices.refresh(index='de_asset_organization')
   print('re-stamped',h[0]['_id'])"
   ```
   (Tracked as a real bug — the add-form selector should win.)

## Running

**On staging (recommended)** — use the wrapper script. It auto-locates
`.env.attack-surface`, refuses to read it if permissions are too loose,
forwards every `TEST_*`/`PERSONA_*` var into `imports_container`, and
runs pytest inside the container so the Python + dependency environment
is guaranteed correct:

```bash
deployment/run-attack-surface-tests.sh
```

### One-time setup — the `attack-surface` command

To run the suite by typing `attack-surface` from anywhere, symlink the
committed wrapper (`deployment/attack-surface`) into `~/.local/bin` (already on
PATH for the ubuntu user). **One manual step per machine** (staging and prod);
the symlink target lives in the repo, so `git pull` keeps it current and a
re-clone doesn't lose it:

```bash
# from the repo root (e.g. ~/kavanah-analytics or ~/DE-Reporting):
ln -s "$(pwd)/deployment/attack-surface" ~/.local/bin/attack-surface
```

After that:

```bash
attack-surface              # staging run (guards active; refuses prod)
attack-surface --prod # deliberate prod run
attack-surface -k SameTitle # extra args forward to pytest
```

The wrapper just locates and exec's `run-attack-surface-tests.sh` — all
prod-gating lives in that one runner, not duplicated in the shim. No cron, no
scheduled runs: the suite runs only when you trigger it.

**Locally** — if you have a working Python env and the suite's deps:

```bash
set -a
source .env.attack-surface
set +a
pytest tests/security/test_attack_surface.py -v -m live
```

At end of run pytest prints the persona × surface matrix and a count of
PASS / LEAK / XFAIL / ERROR / SKIP outcomes.

## Refreshing personas

If a persona's password changes, update `.env.attack-surface` — the suite
re-logs in at start of every run, so cookie expiry is never an issue.

If a forbidden identifier becomes legitimately visible to a persona (e.g.
you grant the team_user a second team), update the persona's `FORBIDDEN_*`
list to match.

## Data Connectors picker surface

`TestDataConnectorsPicker` exercises `/assets/data-connectors/api/wizard/options/<asset_type>`
for each of the `_PICKER_ALLOWED_TYPES` (echo_tenants, chatwoot_accounts,
google_ads_customers, ga_properties, facebook_accounts, facebook_pages,
youtube_accounts) — one cell per type per persona. (facebook_apps +
facebook_app_users were dropped from the route allowlist Phase 3;
youtube_accounts was added. Keep this list in sync with the route's
`_PICKER_ALLOWED_TYPES`.) The picker's "Choose existing account" dropdown must
only show assets in the operator's workspace scope.

The leaked values are **asset titles** (e.g. an Echo tenant title, a Google
Ads customer title), not org/team/workspace identifiers — so this surface has
its own env var: **`PERSONA_<NAME>_FORBIDDEN_ASSET_TITLES`** (comma-separated).
Populate it with the titles of the **control workspace's** connector assets —
the ones a scoped admin must never see in their picker. The test scans for
these titles (unioned with the org/team/workspace forbidden lists in case a
title embeds one of those tokens).

If `FORBIDDEN_ASSET_TITLES` is left empty and the persona still reaches the
picker (HTTP 200), the cell records **SKIP**, not PASS — a green there would be
vacuous (no tokens to detect), and SKIP makes the gap visible in the matrix so
you know to populate the field rather than trusting a misleading pass.

### One-time tripwire setup

The picker leaks **asset titles**, so the tripwire is a connector asset that
lives in the **control workspace** (the one no scoped persona belongs to) with
a distinctive title. An **Echo tenant** is the cheapest tripwire: it's a plain
ES doc with three required fields and **no external API call** at create time
(unlike a Google Ads customer or FB account, which hit live APIs). The fake API
key never leaves staging.

1. **Create the tripwire asset in the control workspace.** It must be stamped
   with the control workspace's `workspace_esid` — and a new asset is stamped
   with the **creator's *primary* workspace** (`resolve_creator_workspace_esid`,
   app.py:1345), NOT a grant or a secondary membership. So create it while
   logged in as a user whose *primary* workspace is the control workspace:
   either a throwaway control-workspace member, or a superuser using the Asset
   Explorer add form's superuser-only workspace field to target the control
   workspace explicitly. (Do **not** create it as `workspace_admin` — it would
   land in *their* workspace and the test would correctly never flag it. And
   note `grant_user`'s control-workspace access is a *grant*, so their primary
   workspace is probably elsewhere — don't assume creating-as-grant_user lands
   in the control workspace; verify the esid in the check below.)

   Asset Explorer → Echo Tenants → Add, with:
   - **Tenant Title**: `ControlOrg Echo Tenant`  ← this exact string becomes the forbidden token
   - **API Key**: any non-empty throwaway (e.g. `attack-surface-tripwire`)
   - **Subdomain**: `controlorg-tripwire`

   Then confirm it carries the control workspace's esid — query ES directly
   and read the `echo_tenant_title` + `workspace_esid` fields out of the hit:
   ```bash
   sudo docker exec imports_container curl -s \
     "http://es01:9200/de_asset_echo_tenants/_search?q=echo_tenant_title:%22ControlOrg%20Echo%20Tenant%22&_source=echo_tenant_title,workspace_esid" \
     | python3 -m json.tool
   ```
   (Adjust the `es01:9200` host/creds to match staging if needed.) The
   `workspace_esid` in the returned hit must be the control workspace's —
   NOT any persona's allowed workspace.

2. **Add the title to every persona that reaches the picker** (holds
   `mc_data_connectors` and is NOT a superuser). In `~/DE-Reporting/.env.attack-surface`:
   ```
   PERSONA_WORKSPACE_ADMIN_FORBIDDEN_ASSET_TITLES=ControlOrg Echo Tenant
   PERSONA_WORKSPACE_MEMBER_FORBIDDEN_ASSET_TITLES=ControlOrg Echo Tenant
   PERSONA_WORKSPACE_GRANT_FORBIDDEN_ASSET_TITLES=ControlOrg Echo Tenant
   ```
   (Match the value to the title you used in step 1, exactly — the scan is
   case-sensitive.)

3. **Run the suite and read the `data_connectors_picker_*` rows in the matrix.**
   ```bash
   cd ~/DE-Reporting
   set -a; source .env.attack-surface; set +a
   pytest tests/security/test_attack_surface.py -m live -k DataConnectorsPicker -v
   ```
   - `data_connectors_picker_echo_tenants` for `workspace_admin` →
     **PASS** (the title is forbidden and correctly absent — the fix works).
   - The other 7 asset-type rows → still **SKIP** unless you also created a
     control-workspace asset of that type (Echo alone is enough to prove the
     narrowing; add others only if you want per-type coverage).
   - **Regression proof (optional):** revert narrowing in
     `list_assets_for_picker` (or temporarily widen it), re-run — the echo row
     must flip to **LEAK**. That confirms the tripwire actually bites.

## Same-title collision

`TestSameTitleCollision` exercises the cross-workspace **title collision** that
both (list surfaces) (resolver) fixed — and that the
original matrix MISSED because every persona's allowed/forbidden titles were
unique. A collision can't be caught by "forbidden title not in response": the
persona's own team and the foreign workspace's team render the **same** string.
The leak shows up as a **duplicate** — two "Acme" rows where the persona should
see only their own.

So this surface uses a count check, not a token scan, via its own env vars:
- **`PERSONA_<NAME>_SAME_TITLE_TEAM`** — the colliding team title (e.g. `Acme`).
- **`PERSONA_<NAME>_SAME_TITLE_EXPECTED_COUNT`** — how many times it should
  appear (default `1` — the persona's own).

If `SAME_TITLE_TEAM` is empty the persona is **SKIPPED** (not PASS) — the suite
is inert until the staging data below exists, never a false green.

### One-time tripwire setup

The tripwire is a **team whose title duplicates one of the persona's own
teams**, living in a workspace the persona has no scope on.

1. **Pick the persona + a title to collide.** `team_user` (or `org_user`) is the
   natural target — they resolve teams through the org→teams path fixed.
   Say their own allowed team is `Acme` in workspace A.

2. **Create the colliding team in the CONTROL workspace.** Stamp it with the
   control workspace's `workspace_esid` (same creator-primary-workspace rule as
   the setup above — create as a control-workspace member or as a
   superuser using the Asset Explorer add form's workspace field). Asset
   Explorer → Teams → Add:
   - **Team Title**: `Acme`  ← exactly matches the persona's own team title
   - **Organization**: an org in the control workspace, ALSO titled `Acme`
     (the org-title collision is what resolver join keyed off).

   Confirm the two `Acme` teams have DISTINCT `_id` / `organization_esid` and
   different `workspace_esid`:
   ```bash
   sudo docker exec imports_container curl -s \
     "http://es01:9200/de_asset_teams/_search?q=team_title:Acme&_source=team_title,organization_esid,workspace_esid" \
     | python3 -m json.tool
   ```
   Two hits, different `workspace_esid` — one the persona's, one the control's.

3. **Set the env var** in `~/DE-Reporting/.env.attack-surface`:
   ```
   PERSONA_TEAM_USER_SAME_TITLE_TEAM=Acme
   # PERSONA_TEAM_USER_SAME_TITLE_EXPECTED_COUNT=1   # default; omit unless >1
   ```

4. **Run and read the `same_title_*` rows:**
   ```bash
   cd ~/DE-Reporting
   set -a; source .env.attack-surface; set +a
   pytest tests/security/test_attack_surface.py -m live -k SameTitleCollision -v
   ```
   - `same_title_filter_options` / `same_title_team_list` → **PASS** (the
     persona sees `Acme` exactly once — their own).
   - **Regression proof:** revert esid join (or
     `filter_hits_by_workspace`) and re-run — the row flips to **LEAK** (`Acme`
     appears 2×). That confirms the tripwire bites.

## Connector configure tenant-scope

`TestConnectorConfigureTenantScope` exercises the 7 connector "configure"
endpoints that gated against tenant scope:
`echo/configure`, `chatwoot/configure`, `youtube/configure`,
`youtube/onboarding-finalize`, `ga4/oauth-configure`,
`ga4/oauth-onboarding-finalize`, `fb/configure`. Pre-fix a holder of
`mc_data_connectors` in workspace A could bind a connector onto a team in
workspace B by POSTing the victim team's esid (cross-tenant write / IDOR).

The attack: each A-scoped persona POSTs `{"team_esid": <Team B1 esid>}` and the
endpoint MUST refuse with **404** (the `_check_team_for_wizard` gate;
401/403/302 also count as denials). A 200 is a LEAK — the configure service was
reached for an out-of-workspace team.

- **Deny-path only.** A 404 persists nothing, so the POST is staging-safe. The
  in-scope happy path would WRITE a connector and mutate staging, so it is NOT
  exercised here — that's covered by the unit + release-plan test
  `CONFIGURE-SCOPE-001`.
- **`grant_user` is skipped** — it's scoped TO Team B1, so the target is
  in-scope and a 404 would be wrong (it's the legitimate-owner control).
- **Superuser** is recorded PASS-by-design (unscoped, sees every team).

### Setup

No new structure — reuses Team B1 (workspace-b) from the matrix above. The
**only** new env var is `TEST_FORBIDDEN_TEAM_ESID`, the raw esid of Team B1.
The seeder resolves `"Team B1"` → esid and writes it on `--apply`; leave it
empty and the surface (and the NLQ surface below) records SKIP, never a
vacuous PASS. The colliding-title trap applies here too: the seeder queries
`team_title.keyword` (text+keyword multi-field — a bare `term` returns 0).

### Run + regression-prove

```bash
cd ~/DE-Reporting
set -a; source .env.attack-surface; set +a
pytest tests/security/test_attack_surface.py -m live -k ConnectorConfigureTenantScope -v
```

All `connector_configure_*` rows for the A-scoped personas → **PASS** (404).
**Regression proof:** drop the `_check_team_for_wizard` call from one endpoint
in `data_connectors/routes.py`, re-run — that endpoint's row flips to **LEAK**
(the configure service is reached and returns 2xx for the foreign team).

## Data Explorer NLQ tenant-scope

`TestDataExplorerNlqTenantScope` exercises `/data-explorer/api/ask` and
`/api/chat`, which changed from prompt-only "targeting hints" to
server-side scope enforcement (the caller's `RoleFilterService` clause AND-ed
into every ES query, deny-all floor for no-scope callers).

The attack: each A-scoped persona asks for Org B / Team B1 data AND passes the
out-of-scope `organizations` / `teams` explicitly. The JSON response
(narrative + stats + table + es_query) MUST contain none of the persona's
forbidden org/team identifiers — `find_forbidden_in_payload` scans the whole
body, so a forbidden token anywhere is a LEAK.

- **READ-only — no staging mutation** — so unlike the configure surface, the
  scoped personas ARE exercised directly (that's the main fix).
- **Live model calls.** `/api/ask` and `/api/chat` invoke the NLQ model, so the
  surface SKIPs cleanly when NLQ is disabled (503), the API key is absent
  (502), or the caller is denied before NLQ (no `mc_data_explorer`).
- **`bare`** (no resolvable scope) exercises the deny-all floor — its forbidden
  set spans every org/team, so a clean scan proves it reads nothing.
- **Superuser** is PASS-by-design (unscoped, no forbidden set).

### Run + regression-prove

```bash
pytest tests/security/test_attack_surface.py -m live -k DataExplorerNlqTenantScope -v
```

`nlq_ask_scope` / `nlq_chat_scope` rows → **PASS** (or SKIP if NLQ is off on
staging). **Regression proof:** drop `_apply_scope_filter` at the
`search_elasticsearch` site in `nlq_service.py`, re-run with NLQ enabled — the
row flips to **LEAK** (Org B / Team B1 data appears in the response).

> **Dormant by design (2026-06-16).** NLQ is not in production use, and none of
> the 10 personas carries `db_data_explorer`/`mc_data_explorer`, so these two
> rows **SKIP on every run** ("denied before NLQ — no mc_data_explorer"). That
> is expected, not a coverage gap: the surface is a pre-wired tripwire that
> activates the moment (a) NLQ is enabled in real use and (b) a persona is given
> a `db_data_explorer`-bearing role scoped to Org A. Until then rests on
> its mutation-verified unit suite (`NLQ-SCOPE-001`). To activate: define
> a minimal `data_explorer_user` role (only `db_data_explorer: True` — NOT the
> privileged `asset_admin`, which is in `_DATA_PRIVILEGED_ROLES`) and assign it
> to an Org-A-scoped persona; it then narrows to Org A and a leak is detectable.

## When a surface stops being xfail

The suite has three XFAIL surfaces tied to filed tickets:

- `dashboard_menus` →
- `asset_explorer_data_sources` →
- `user_management_user_list` (workspace_admin row only) →

When any ticket lands, the matching test starts passing (XPASS). When you
see XPASS in the matrix:

1. Remove the `@pytest.mark.xfail` decorator from the test class.
2. Replace the `XFAIL` outcome wrapper with a real `assert outcome.outcome == "PASS"`.
3. Commit alongside the ticket fix.

## Prod safeguard + opt-in

Both the runner script and the suite's `conftest.py` hard-refuse a prod
hostname (`kavanahanalytics.com`, plus anything listed in `PROD_HOSTNAMES`) **by
default**. stood the suite up on prod with dedicated throwaway personas,
so there is now a deliberate, two-part opt-in:

- Pass `--prod` to the runner / `attack-surface` shim, **and**
- the runner exports `ATTACK_SURFACE_ALLOW_PROD=1` into the container, which is
  the only thing that relaxes the conftest guard.

Both must agree: a stray env var alone won't reach prod (no `--prod` ⇒ the
runner errors before pytest), and a stray flag alone won't (the conftest still
refuses unless the var is set). CI never sets either, so CI stays staging-only.
The seeder (`seed_attack_surface_personas.py`) honors the same opt-in before it
will create accounts on a prod hostname.
