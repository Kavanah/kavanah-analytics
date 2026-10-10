# Kavanah Analytics — Deployment Scripts

Scripts for installing, operating, and rolling back the Kavanah Analytics stack
on Ubuntu 24.04. Two families live here:

- **`step*.sh`** — first-run bootstrap for a fresh VM (base packages, Docker,
  master config, initial stack bring-up, nginx). Run once in order.
- **`0N_*.sh`** — idempotent, repeatable automation for day‑2 operations
  (build/push, redeploy, cron, seed, health, rollback). Run as needed.

---

## Prerequisites

| Requirement | Value |
|---|---|
| OS | Ubuntu 24.04 LTS |
| RAM | **8 GB** minimum (Elasticsearch may OOM below this) |
| Disk | 40 GB+ free on `/` (200 GB recommended for long-running installs) |
| Kernel | `vm.max_map_count ≥ 262144` (step1 sets this) |
| Network | Outbound HTTPS to `docker.elastic.co`, `github.com`, ECR (if used) |
| User | Non-root with `sudo` (scripts call `sudo` internally) |

---

## First-run install on a fresh VM

```bash
# 1. Clone the public deploy repo
git clone git@github.com:Kavanah/kavanah-analytics.git ~/kavanah-analytics
cd ~/kavanah-analytics

# 2. Base packages + Docker + repo sanity
bash deployment/step1-and-2-base-setup.sh

# 3. First-run bootstrap: creates master-config.yaml, .env.test,
#    generates docker-compose.yml, starts ES / Kibana / Flask apps
bash deployment/step3-app-stack.sh

# 4. (recommended) Build the Sphinx user docs.
#    Creates a venv at ~/docs-venv and populates docs/documentation/_build/html
#    which nginx (step6) mounts at /var/www/deam-docs for the /docs/ route.
bash deployment/step7-build-docs.sh

# 5. (optional) nginx reverse proxy for HTTPS.
#    Mounts docs automatically if step7 ran; mounts /var/www/marketing too
#    if present (see "Marketing content" below).
bash deployment/step6-nginx-setup.sh
```

After step 3 you have a working stack. The `0N_*.sh` scripts below take over
from there.

### Deploy-time hostname variables

`deployment/.env` (written by step3, substituted into `imports_container` by
`docker-compose.yml`) carries three optional settings that used to be literals
in the tree. Export them before running step3, or edit `.env` and re-create the
container (`docker compose -f deployment/docker-compose.yml up -d --force-recreate imports_container`).

| Variable | Scope | Meaning |
|---|---|---|
| `TEST_PERSONA_DOMAIN` | staging only | Domain of the seeded `test-<slug>@<domain>` header-auth personas. Blank = `example.com`, so set it on staging before the E2E lane runs. |
| `PROD_HOSTNAMES` | optional | Comma-separated extra hostnames the refuse-prod guards treat as production, on top of the built-in `kavanahanalytics.com`. |
| `APP_PUBLIC_HOSTNAME` | optional | Public hostname `scripts/check_oauth_tokens.py` uses to print full refresh URLs. step3 defaults it to `reporting.hostname`; blank prints relative paths. |

Also: the Testing page's load-test target now defaults to the app's own host
(`request.host_url`). It used to default to the staging URL, so a prod run
now hits prod itself unless `LOAD_TEST_TARGET_URL` says otherwise.

### Marketing content (optional, private only)

The public landing page, pricing page, and feature detail pages are **not in
this repo** — they live in the private `digital-engagement-internal`
submodule under `the private docs`. If you're deploying the private build:

```bash
# On your workstation (not the server), with the submodule checked out:
cd the private docs
export MARKETING_SSH_TARGET="ubuntu@<host>:/var/www/marketing/"
./deploy.sh
```

Then re-run `deployment/step6-nginx-setup.sh` on the server so nginx picks
up the new mount. On public-only installs the marketing mount is skipped
and `/` returns 404 — users typically access the app directly via
`/dashboard` or `/login`.

### Rebuilding docs after updates

```bash
bash deployment/step7-build-docs.sh   # re-run anytime; idempotent
```

The script reuses the existing venv. nginx sees the updated HTML
immediately via the bind mount — no container restart needed.

---

## Day‑2 automation (01 → 06)

All scripts live in `deployment/`. All exit non-zero on any failure and are
safe to re-run.

### `01_build_images.sh` — Build + push to ECR

Builds the `de-imports` image (from `bin/python/Dockerfile`), tags it with
the 12-char git SHA plus `:latest`, and pushes to ECR.

```bash
bash deployment/01_build_images.sh            # build + push to ECR
bash deployment/01_build_images.sh --local    # build only, skip ECR
```

ECR settings come from env vars or `config/master-config.yaml`:
```yaml
deploy:
  aws:
    ecr_registry: 123456789012.dkr.ecr.us-east-1.amazonaws.com
    region: us-east-1
  images:
    imports_repo: de-imports
```

The nginx reverse-proxy image is **not** built by this script. Its
Dockerfile bakes per-VM credentials (`.htpasswd` from the elastic password
in `master-config.yaml`) into the image at build time, which makes it
non-portable. nginx is built and managed per-VM by
`deployment/step6-nginx-setup.sh`. A future refactor to runtime-mount
`.htpasswd` would let 01 build it too — tracked as a follow-up.

### `02_deploy_stack.sh` — Reconcile stack + wait for health

Runs `docker compose up -d` on `config/docker-compose.yml` (generated by
step3) and polls until Elasticsearch, Kibana, and all three Flask ports
(8083/8084/8085) respond healthy.

```bash
bash deployment/02_deploy_stack.sh                  # default: wait up to 300s each
bash deployment/02_deploy_stack.sh --no-wait        # start containers only
bash deployment/02_deploy_stack.sh --timeout 600    # longer poll
```

### `03_provision_cron_jobs.sh` — DEAM scheduled jobs

Installs a managed block of cron entries (delimited by marker comments) for
every job DEAM owns on a schedule. `SCHEDULE` in the script is the
authoritative list; today that is the token refresh, the two demo-data
jobs (through `tools/run_tracked_job.py`), the trial-ending email,
the report-scheduler poll runner, the deferred-build sweep, the credential-
health probe and the two retention sweeps (, provisioned
without `--apply` on purpose). Every entry appends to `/var/log/<slug>.log`
 and the matching logrotate configs are installed alongside.

**The per-importer cron lines are gone.** `import.py <connector>
run_daily_imports` for facebook, google_ads, google_analytics, echo, youtube,
chatwoot and the disciple_tools pull importer migrated to the report scheduler
at and are no longer provisioned here.

**Run it with `sudo`.** The managed block belongs to root's crontab. `crontab`
in the script is bare, so a run as another user does not fail — it installs a
SECOND managed block in that user's spool and every job then fires twice
. `--install` and `--uninstall` refuse to run as anyone but root for
that reason; the read-only modes do not.

```bash
sudo bash deployment/03_provision_cron_jobs.sh            # install (idempotent)
sudo bash deployment/03_provision_cron_jobs.sh --check    # installed block == this script?
sudo bash deployment/03_provision_cron_jobs.sh --uninstall
bash deployment/03_provision_cron_jobs.sh --dry-run       # preview; no sudo needed
```

Re-running atomically replaces the managed block — never creates duplicates.

`start-imports-container.sh` runs the install and then `--check` on every
deploy, so this is no longer a step anyone has to remember. It had
been one, and staging's block was consequently three weeks and three tickets
behind the script. `--check` reads the *invoking* user's crontab, so a non-root
`--check` says nothing about root's block — it warns and tells you to re-run
with sudo.

### `04_seed_test_data.sh` — Seed Elasticsearch with test data

Runs `bin/python/seed_test_data.py` inside `imports_container`. Skips seeding
if the six `test_*` indices are already populated.

```bash
bash deployment/04_seed_test_data.sh          # skip populated indices
bash deployment/04_seed_test_data.sh --force  # delete + recreate
```

### `05_health_check.sh` — Stack health probe

Single-pass, read-only probe. Exits 0 if healthy, 1 with a breakdown
otherwise. Safe to run at deploy time or from cron as a monitor.

```bash
bash deployment/05_health_check.sh
bash deployment/05_health_check.sh --skip-nginx   # test VM without step6
bash deployment/05_health_check.sh --quiet        # failures only
```

### `06_rollback.sh` — Roll back to a prior version

Accepts a git SHA (short or full) or a git tag that was pushed by
`01_build_images.sh`. Pulls the matching image tags, re-tags them as
`:latest`, restarts the stack, and re-runs `05_health_check.sh`.

```bash
bash deployment/06_rollback.sh <target>            # pull from ECR
bash deployment/06_rollback.sh <target> --local    # git checkout + rebuild
```

Rolling back to the currently-running tag is a no-op (still runs the health
check).

---

## One-time data migrations

Some commits change ES state (security roles, saved-object schemas) in ways
a `git pull` alone cannot apply. These steps must be run on each long-lived
environment exactly once, in the order listed below. **Do not skip steps and
do not reorder them** — the order is what keeps users from being locked out
of Kibana mid-deploy.

### Team-analytics → default consolidation

Required when deploying commit `feat(kibana): consolidate to default space
(Kavanah Analytics)` (87de9b8) or later to an environment that still has
the `team-analytics` Kibana space. Two steps, in this order:

**Step 1 — Rewrite ES security roles (required).**
Every existing `org_*` and `team_*` role currently grants access to
`space:team-analytics`. Rewrite them to `space:default` first, before
deleting the space, or every non-admin user loses Kibana access:

```bash
PASS=$(sudo docker inspect imports_container \
    --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | grep ES01_PASSWORD= | cut -d= -f2-)

ESPASS="$PASS" python3 deployment/migrate-roles-to-default-space.py --dry-run
ESPASS="$PASS" python3 deployment/migrate-roles-to-default-space.py
```

The script is idempotent (re-run prints "Already migrated") and preserves
each role's privilege set. It only rewrites the resources list.

**Step 2 — Delete orphan dashboard configs in `de_asset_dashboards` (required).**
On environments that ran `update_v12.py` historically, the
`de_asset_dashboards` ES index has hidden orphan records pointing at
`team-analytics` (auto-generated Kibana saved objects pulled in at the time).
Their `visibility_mode` is `hide` so users never see them, but they will
dangle after the space is deleted. Remove them:

```bash
curl -sk -u "elastic:$PASS" -H 'Content-Type: application/json' \
    -X POST "https://localhost:9200/de_asset_dashboards/_delete_by_query?refresh=true" \
    -d '{"query":{"term":{"space_id.keyword":"team-analytics"}}}'
# expect "deleted":N where N is small (1 on dev-reporting)
```

Idempotent — re-running prints `"deleted":0`.

**Step 3 — Delete the unused team-analytics space (required).**
With roles and orphan docs cleaned up, the `team-analytics` Kibana space
is unused and must be removed so the environment matches the new code's
assumption of a single space:

```bash
curl -sk -X DELETE -u "elastic:$PASS" -H 'kbn-xsrf: true' \
    "http://localhost:5601/kibana/api/spaces/space/team-analytics"
# expect HTTP 204
```

The DELETE cascade-removes the space's data views, tags, and configs.
The default space (display name "Kavanah Analytics") keeps its own copy
of the same 10 data views — built from `deployment/create-data-views.sh`.

**Status by environment:**

| Environment | Step 1 (roles) | Step 2 (orphan docs) | Step 3 (space delete) |
|---|---|---|---|
| `dev-reporting` | ✅ done 2026-04-27 | ✅ done 2026-04-27 | ✅ done 2026-04-27 |
| production (`kavanahanalytics.com`) | ⏳ pending | ⏳ pending | ⏳ pending |

**Doing step 3 without step 1 will lock every non-admin user out of Kibana
on that environment.** Always run them in order.

**Rollback:** if step 3 was somehow done before step 1, recreate the space
and re-run `bash deployment/create-data-views.sh` (an older version that
still targets both spaces) before fixing the roles. Easier path: edit
`migrate-roles-to-default-space.py` to swap source/target strings and
re-run, then provision a fresh `team-analytics` space if needed.

---

## Dashboard templates are source-controlled

`bin/python/dashboard_templates/*.json` is the source of truth for the
`de_dashboard_templates` index (filename stem = ES `_id`). You do not run a
deploy step for them: the web app aligns ES to git **on every start** via
`tools.sync_dashboard_templates.sync_on_startup()` (idempotent, fail-soft;
each overwrite of a live doc that differed from git is logged at WARNING in
the imports_container log).

Consequences to know before touching the index by hand:

- **A template deleted or edited directly in ES is resurrected / reverted by
  the next container start.** To retire a template, delete its JSON file in
  git and deploy; the ES doc then shows as `not_in_git` (reported, never
  deleted) until removed by hand. To change one, edit the JSON in git.
- To adopt a live edit into git instead:
  `docker exec -w /app/python imports_container python3 -m tools.sync_dashboard_templates --export --only <id>`,
  then commit the file.
- Drift check (read-only): `python3 -m tools.sync_dashboard_templates`
  exits `0` in sync, `2` on drift, `1` if ES is unavailable or the read was
  partial — so it can gate a script and re-alarms on every run.

---

## Idempotency

Every `0N_*.sh` script can be re-run without side effects:

| Script | What makes it idempotent |
|---|---|
| 01 | Docker build cache + overwriting a tag in ECR is a no-op |
| 02 | `docker compose up -d` reconciles declared vs actual state |
| 03 | Managed block replaced atomically via begin/end marker comments |
| 04 | Skips any test index that already has documents |
| 05 | Read-only by construction |
| 06 | No-ops when running image ID already matches target |

---

## Known deviations

See [`DEVIATIONS.md`](DEVIATIONS.md) for the accepted drift list (OS version,
RAM floor, etc.) — resolve before production provisioning.
