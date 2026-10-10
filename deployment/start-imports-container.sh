#!/bin/bash
#
# start-imports-container.sh — bounce imports_container on prod with
# AWS credentials freshly fetched from SSM.
#
# WHY a wrapper:
#   The Restore from Backup page in DEAM needs S3 read access to
# s3://km-wp-backups/elastic-stack-prod/{data,config}/ (; the
#   app user also keeps its own writable app/ prefix). The credentials live in
#   AWS SSM Parameter Store, fetched on the host via the km-infra-managed
#   helper at /usr/local/bin/ssm-get-creds (configured as
#   credential_process under [profile app-backups] in /root/.aws/config).
#
#   We can't bake the keys into the de-imports image (shared with dev/test),
#   and we don't bind-mount /root/.aws/ into the container because that
#   would expose the bootstrap-user SSM read scope (everything under
#   /km/prod/*), not just app-backups. Instead we fetch creds on the host,
#   write them to deployment/.env.aws (mode 600), and let compose's
#   `env_file:` directive pick them up — boto3 reads AWS_ACCESS_KEY_ID
#   and AWS_SECRET_ACCESS_KEY from process env automatically.
#
# WHEN to run:
#   - First time prod is set up for S3 restore.
#   - After km-infra rotates the app-backups credential in SSM.
#   - Any other time imports_container needs to come back up with a
#     refreshed AWS credential.
#
# What changed at (2026-05-09):
#   The wrapper used to do its own `docker run`, which put it out of
# sync with the canonical compose file and required manual
#   parity tracking. Now the wrapper writes credentials to .env.aws and
#   delegates to `docker compose up -d imports_container`. Compose
#   substitutes from deployment/.env (step3-managed) plus
#   deployment/.env.aws (this wrapper) — single declarative source of
#   truth.
#
# What changed for Ask Kavanah / Bedrock (2026-07-13):
#   The wrapper now also fetches the NLQ Bedrock credentials from SSM
#   (/km/<env>/bedrock-nlq/*) via /usr/local/bin/ssm-get-creds and writes
#   them into .env.aws as NLQ_AWS_ACCESS_KEY_ID / NLQ_AWS_SECRET_ACCESS_KEY.
#   The app passes these explicitly to AnthropicBedrock (nlq_service.py) —
#   no [bedrock] profile file on the host any more. The wrapper now runs on
#   BOTH boxes: `--env prod` (default, unchanged prod runbook + bedrock
#   fetch) or `--env dev` (skips app-backups — that profile only exists on
#   prod — and uses the dev box's [bootstrap] SSM reader for the bedrock
#   fetch). A failed bedrock fetch WARNS and continues: NLQ is a feature,
#   not the app — its misconfiguration must never block container start.
#
# This is a hand-run wrapper — not auto-invoked by step3 or systemd.
# Run it as the ubuntu user WITHOUT an outer sudo (snap-confined
# yq fails under sudo). The wrapper internally sudo-escalates the AWS SSM
# credential fetches and the cron provisioning (that block lives in
# root's crontab).

set -euo pipefail

# ── Arguments ─────────────────────────────────────────────────────────────────
# --env dev|prod (default prod — preserves the existing prod runbook usage).
ENV="prod"
while [ $# -gt 0 ]; do
  case "$1" in
    --env)   ENV="${2:-}"; shift 2 ;;
    --env=*) ENV="${1#*=}"; shift ;;
    *) echo "[ERROR]   Unknown argument: $1 (usage: $0 [--env dev|prod])" >&2; exit 1 ;;
  esac
done
case "$ENV" in
  prod) SSM_BOOTSTRAP_PROFILE="default" ;;    # prod box: the SSM reader IS [default]
  dev)  SSM_BOOTSTRAP_PROFILE="bootstrap" ;;  # dev box: the reader is [bootstrap]
  *) echo "[ERROR]   --env must be dev or prod (got: $ENV)" >&2; exit 1 ;;
esac

# ── Configuration ─────────────────────────────────────────────────────────────
REPO_DIR="${REPO_DIR:-/home/ubuntu/kavanah-analytics}"
DEPLOY_DIR="$REPO_DIR/deployment"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.yml"
COMPOSE_AWS_ENV="$DEPLOY_DIR/.env.aws"
COMPOSE_ENV="$DEPLOY_DIR/.env"
AWS_PROFILE_NAME="app-backups"
SSM_GET_CREDS="/usr/local/bin/ssm-get-creds"
NLQ_SSM_PREFIX="/km/${ENV}/bedrock-nlq"

# ── Helpers ───────────────────────────────────────────────────────────────────
info()    { echo "[INFO]    $*"; }
success() { echo "[SUCCESS] $*"; }
warn()    { echo "[WARN]    $*" >&2; }
error()   { echo "[ERROR]   $*" >&2; exit 1; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || error "Required command not found: $1"
}

# ── Pre-flight ────────────────────────────────────────────────────────────────
require_cmd aws
require_cmd docker
require_cmd jq
docker compose version >/dev/null 2>&1 || error "Docker Compose v2 plugin missing."

[[ -f "$COMPOSE_FILE" ]] || error "Compose file not found at $COMPOSE_FILE — run step3-app-stack.sh first."
[[ -f "$COMPOSE_ENV" ]] || error "Compose env file not found at $COMPOSE_ENV — run step3-app-stack.sh first."

# ── 1. Fetch S3 backup credentials (prod only — app-backups profile) ──────────
AWS_CREDS=""
if [ "$ENV" = "prod" ]; then
    info "Fetching AWS credentials for profile '$AWS_PROFILE_NAME' from SSM..."
    AWS_CREDS=$(sudo aws --profile "$AWS_PROFILE_NAME" configure export-credentials --format env-no-export 2>&1) \
        || error "Failed to fetch AWS credentials: $AWS_CREDS"

    # Sanity-check: at minimum AWS_ACCESS_KEY_ID must be present. AWS_SESSION_TOKEN
    # is only emitted for STS-issued creds; long-lived IAM keys (current state)
    # return only AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY.
    if ! grep -q '^AWS_ACCESS_KEY_ID=' <<< "$AWS_CREDS"; then
        error "AWS export-credentials returned no AWS_ACCESS_KEY_ID — check the profile config"
    fi
    success "AWS credentials fetched"
else
    info "Skipping app-backups fetch (--env dev: no S3 restore on this box)"
fi

# ── 1b. Fetch NLQ / Ask Kavanah Bedrock credentials from SSM ──────────────────
# Uses the km-infra ssm-get-creds helper (same one the [app-backups] and
# [s3-backup] credential_process profiles use). Output is captured, never
# displayed — only structural facts are logged. Failure is a WARN, not an
# error: the container must still start if the NLQ params are missing.
NLQ_ACCESS_KEY=""
NLQ_SECRET_KEY=""
if sudo test -x "$SSM_GET_CREDS"; then
    info "Fetching Bedrock NLQ credentials from SSM (${NLQ_SSM_PREFIX})..."
    if NLQ_CREDS_JSON=$(sudo env AWS_SSM_GETCREDS_PROFILE="$SSM_BOOTSTRAP_PROFILE" "$SSM_GET_CREDS" "$NLQ_SSM_PREFIX" 2>/dev/null); then
        NLQ_ACCESS_KEY=$(jq -r '.AccessKeyId // empty' <<< "$NLQ_CREDS_JSON")
        NLQ_SECRET_KEY=$(jq -r '.SecretAccessKey // empty' <<< "$NLQ_CREDS_JSON")
        unset NLQ_CREDS_JSON
        if [[ "$NLQ_ACCESS_KEY" =~ ^AKIA[A-Z0-9]{16}$ ]] && [ ${#NLQ_SECRET_KEY} -eq 40 ]; then
            success "Bedrock NLQ credentials fetched (structural check passed)"
        else
            warn "Bedrock NLQ credentials failed structural check — NLQ vars will not be written"
            NLQ_ACCESS_KEY=""; NLQ_SECRET_KEY=""
        fi
    else
        warn "Could not fetch ${NLQ_SSM_PREFIX}/* from SSM — Ask Kavanah (Bedrock) will be unavailable"
    fi
else
    warn "$SSM_GET_CREDS not found — skipping Bedrock NLQ credential fetch"
fi

# ── 1c. Fetch password-reset SMTP credentials from SSM ──────────────
# SES SMTP creds for bin/python/password_reset (username = access key id,
# password = the DERIVED 44-char SES SMTP password, not a raw IAM secret —
# which is why this uses plain get-parameters instead of ssm-get-creds, whose
# structural validation expects raw AWS keypairs). Same warn-don't-fail
# behavior as the Bedrock fetch: a missing credential must not block startup.
PWRESET_SMTP_USER=""
PWRESET_SMTP_PASS=""
SMTP_SSM_PREFIX="/km/${ENV}/deam/pw-reset-smtp"
SSM_REGION="us-west-2"
info "Fetching password-reset SMTP credentials from SSM (${SMTP_SSM_PREFIX})..."
if SMTP_JSON=$(sudo aws ssm get-parameters \
        --profile "$SSM_BOOTSTRAP_PROFILE" --region "$SSM_REGION" \
        --names "${SMTP_SSM_PREFIX}/username" "${SMTP_SSM_PREFIX}/password" \
        --with-decryption --output json 2>/dev/null) \
   && [ "$(jq -r '.InvalidParameters | length' <<< "$SMTP_JSON")" -eq 0 ]; then
    PWRESET_SMTP_USER=$(jq -r --arg n "${SMTP_SSM_PREFIX}/username" '.Parameters[] | select(.Name==$n).Value' <<< "$SMTP_JSON")
    PWRESET_SMTP_PASS=$(jq -r --arg n "${SMTP_SSM_PREFIX}/password" '.Parameters[] | select(.Name==$n).Value' <<< "$SMTP_JSON")
    unset SMTP_JSON
    if [[ "$PWRESET_SMTP_USER" =~ ^AKIA[A-Z0-9]{16}$ ]] && [ ${#PWRESET_SMTP_PASS} -eq 44 ]; then
        success "Password-reset SMTP credentials fetched (structural check passed)"
    else
        warn "Password-reset SMTP credentials failed structural check — vars will not be written"
        PWRESET_SMTP_USER=""; PWRESET_SMTP_PASS=""
    fi
else
    unset SMTP_JSON
    warn "Could not fetch ${SMTP_SSM_PREFIX}/* from SSM — password-reset email will fall back to settings.json"
fi

# ── 2. Write credentials to deployment/.env.aws (compose env_file source) ────
# mode 600 because this file contains live AWS creds. compose reads it on
# every `up -d`, so the next imports_container start will inject these env
# vars without exposing them via `ps` (which `docker run --env-file` would
# also avoid, but the compose-native env_file: is cleaner).
info "Writing $COMPOSE_AWS_ENV..."
{
    echo "# AWS credentials for imports_container."
    echo "# Written by deployment/start-imports-container.sh on $(date -u +'%Y-%m-%dT%H:%M:%SZ') (--env ${ENV})."
    echo "# Read by compose via env_file: directive in deployment/docker-compose.yml."
    echo "# Rotate by re-running the wrapper; do not edit by hand."
    if [ -n "$AWS_CREDS" ]; then
        echo "$AWS_CREDS"
    fi
    if [ -n "$NLQ_ACCESS_KEY" ] && [ -n "$NLQ_SECRET_KEY" ]; then
        echo "NLQ_AWS_ACCESS_KEY_ID=${NLQ_ACCESS_KEY}"
        echo "NLQ_AWS_SECRET_ACCESS_KEY=${NLQ_SECRET_KEY}"
    fi
    if [ -n "$PWRESET_SMTP_USER" ] && [ -n "$PWRESET_SMTP_PASS" ]; then
        echo "PW_RESET_SMTP_HOST=email-smtp.${SSM_REGION}.amazonaws.com"
        echo "PW_RESET_SMTP_PORT=587"
        echo "PW_RESET_SMTP_USERNAME=${PWRESET_SMTP_USER}"
        echo "PW_RESET_SMTP_PASSWORD=${PWRESET_SMTP_PASS}"
    fi
} > "$COMPOSE_AWS_ENV"
chmod 600 "$COMPOSE_AWS_ENV"
unset NLQ_ACCESS_KEY NLQ_SECRET_KEY PWRESET_SMTP_USER PWRESET_SMTP_PASS
success "$COMPOSE_AWS_ENV written (mode 600)"

# ── 3a. Rebuild the image before recreating ─────────────────────────
# `up -d --force-recreate` reuses whatever de-imports:latest already exists on the
# host; nothing else in the release flow ever built one. Prod ran a 2026-08-04
# image and staging a 2026-06-26 one while the code (bind-mounted) moved on, so
# the hashed lockfile never reached either host. Building here is a
# few seconds when Dockerfile / requirements.txt are unchanged (layer cache) and
# a real rebuild when they are. A hash mismatch fails the deploy loudly — fix it
# by recompiling requirements.txt from requirements.in, never by hand-editing
# the lock. No --pull: refreshing the python:3.13-slim base stays a deliberate
# operator action.
image_provenance() {
    docker image inspect "$(docker compose -f "$COMPOSE_FILE" --env-file "$COMPOSE_ENV" \
        config --images 2>/dev/null | grep -m1 -E '^de-imports|imports' || echo de-imports:latest)" \
        --format 'image {{.Id}} created {{.Created}}' 2>/dev/null || echo "image (none yet)"
}
info "imports image before build: $(image_provenance)"
info "Building imports_container image via docker compose (cache-aware)..."
docker compose -f "$COMPOSE_FILE" --env-file "$COMPOSE_ENV" build imports_container \
    || error "image build failed — if pip reported a hash mismatch, recompile bin/python/requirements.txt from requirements.in; do not hand-edit the lock"
success "imports image after build:  $(image_provenance)"

# ── 3. Bounce imports_container via compose ──────────────────────────────────
# `up -d --force-recreate` so the new .env.aws actually gets picked up.
# Without --force-recreate, compose would no-op if the service config in
# the compose file hasn't changed — env_file: contents alone don't trigger
# a recreate. The recreate is fast (~5s); SIGTERM trap means the
# old container drains cleanly within Docker's 10s grace.
info "Recreating imports_container via docker compose..."
docker compose -f "$COMPOSE_FILE" --env-file "$COMPOSE_ENV" \
    up -d --force-recreate imports_container

success "imports_container recreated with refreshed AWS credentials."

# ── 4. Rebuild the published docs ─────────────────────────────────
# `git pull` updates the docs SOURCE and changes nothing about what /docs/
# serves: nginx bind-mounts a gitignored build directory. Nothing in the release
# flow ever rebuilt it, so prod served a build dated 2026-08-31 through three
# cutovers — two weeks and four doc commits of drift. The drift is not
# symmetric: a DELETED page or image keeps being served, which is how
# redacted screenshots stayed publicly readable after removal from the repo.
#
# Deliberately AFTER the recreate: the app is the product, and a Sphinx failure
# must not keep imports_container down. It still exits non-zero so the deploy
# fails loudly rather than drifting quietly — the whole point of the ticket.
#
# Only THIS wrapper, not start-secrets-container.sh: the prod cutover runbook
# calls this one, while the secrets wrapper is a credential-rotation path (run
# after km-infra rotates an SSM param) where no `git pull` is implied and the
# tree has not moved. Unlike the image build, stale docs are a function
# of the checkout advancing, not of the container being recreated.
info "Rebuilding published docs..."
REPO_DIR="$REPO_DIR" bash "$DEPLOY_DIR/step7-build-docs.sh" \
    || error "docs rebuild failed — /docs/ is still serving the previous build"
REPO_DIR="$REPO_DIR" bash "$DEPLOY_DIR/step7-build-docs.sh" --check \
    || error "docs build stamp does not match the checkout"
success "Published docs rebuilt from the current checkout."

# ── 5. Provision the cron block ───────────────────────────────────
# 03_provision_cron_jobs.sh is the authoritative definition of the DEAM cron
# block and is idempotent, but until now nothing called it: it was named only in
# deployment/README.md and the runbooks as a hand-run step, so
# the crontab advanced only when someone remembered. Staging's block was last
# provisioned before 2026-08-27 — three weeks behind, missing credential_health_probe
# and both retention jobs outright, and still running the two
# demo jobs in their earlier untracked form, so those recorded nothing to
# de_job_health. Same defect class as, and the same fix: a deploy step,
# not a runbook line.
#
# sudo, deliberately: the managed block belongs to root's crontab and the script's
# `crontab` call is bare, so running it as ubuntu would not fail — it would install
# a parallel block in the ubuntu spool and every job would fire twice.
# The script refuses a non-root install for that reason; this is the escalation it
# expects.
#
# After the recreate, for the the app is the product, and a
# crontab failure must not keep imports_container down. It still fails the deploy
# loudly rather than drifting quietly — drifting quietly is the whole ticket.
#
# --check afterwards proves the block the machine holds is the block the script
# generates. Provisioning is idempotent, so that is the only post-condition worth
# asserting — and it is what catches an install that landed in the wrong spool.
info "Provisioning the DEAM cron block..."
sudo bash "$DEPLOY_DIR/03_provision_cron_jobs.sh" \
    || error "cron provisioning failed — the crontab still holds the previous block"
sudo bash "$DEPLOY_DIR/03_provision_cron_jobs.sh" --check \
    || error "installed cron block does not match the script after provisioning"
success "DEAM cron block provisioned and verified against the script."

# ── 6. Provision the ES snapshot repo + SLM policy ─────────────────
# 09_provision_slm.sh is the authoritative definition of the `daily_repo`
# snapshot repository and the `daily-snapshots` SLM policy. Until the
# policy existed only as hand-created prod cluster state: nothing in git
# defined it, and staging never got one at all — the retention sweep's SLM
# check returned NotFoundError there from the day it shipped, and
# the encrypted-restore check ENC-M01 had no snapshots to restore.
# Same defect class as, same fix: a deploy step.
#
# NOT sudo'd, deliberately: unlike the cron block this is ES REST calls only,
# run as the elastic superuser over localhost. The script fetches the elastic
# password itself (env-first, master-config, then SSM — it internally sudos
# the SSM read on the -stripped prod box).
#
# After the recreate, for the the app is the product, and an
# SLM failure must not keep imports_container down. It still fails the deploy
# loudly — a cluster quietly holding no snapshot policy is staging's whole
# history.
#
# --check afterwards proves the cluster holds the repo and policy this script
# defines; registration is idempotent, so that is the only post-condition
# worth asserting.
info "Provisioning the ES snapshot repository + SLM policy..."
bash "$DEPLOY_DIR/09_provision_slm.sh" --env "$ENV" \
    || error "SLM provisioning failed — the cluster still holds the previous snapshot state"
bash "$DEPLOY_DIR/09_provision_slm.sh" --env "$ENV" --check \
    || error "live SLM state does not match the script after provisioning"
success "ES snapshot repository + SLM policy provisioned and verified."
echo ""
echo "Verify:"
if [ "$ENV" = "prod" ]; then
    echo "  docker exec imports_container printenv AWS_ACCESS_KEY_ID | head -c4; echo  # 'AKIA' = S3 restore creds present"
fi
echo "  docker exec imports_container printenv NLQ_AWS_ACCESS_KEY_ID | head -c4; echo  # 'AKIA' = Bedrock NLQ creds present"
echo "  docker exec imports_container printenv PW_RESET_SMTP_USERNAME | head -c4; echo  # 'AKIA' = pw-reset SMTP creds present"
echo "  docker compose -f $COMPOSE_FILE ps imports_container       # should show running + healthy"
