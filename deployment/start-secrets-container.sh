#!/bin/bash
#
# start-secrets-container.sh — recreate imports_container with config secrets
# freshly fetched from AWS SSM Parameter Store.
#
# WHY a wrapper (same rationale as start-imports-container.sh /):
#   master-config.yaml holds the app's secrets (enc_key, Flask secret, ES admin
# password, GitHub token, plus Yubico/Anthropic/Jira/Stripe). moves
#   those into AWS SSM SecureString params so they no longer live in the YAML
#   (Phase 3 strips the YAML values). The CONTAINER never talks to SSM — that
#   would mean putting an SSM-capable AWS credential inside it (bootstrap secret
#   + over-broad blast radius). Instead the HOST fetches the params under a
#   scoped identity and writes them into deployment/.env, which compose already
#   substitutes into imports_container's environment (${ENC_KEY} etc.). The
#   container is identical to today from its own perspective — just env vars.
#
#   WHY .env and not a separate .env.secrets: compose's `environment:` attribute
#   OVERRIDES `env_file:` (even for empty values), and the migrated secrets are
#   declared in `environment:` as ${VAR} substitutions from .env. A parallel
#   env_file: could therefore never win. Writing the values into the same .env
#   compose already substitutes from is the one place with no precedence
#   conflict. This is the SECRETS sibling of start-imports-container.sh (which
#   writes S3 creds into .env.aws) — kept as a separate wrapper on purpose: S3
# creds and config secrets have different rotation owners/cadences (
#   plan, open-question #3).
#
# WHEN to run:
#   - After Phase 3 strips a secret from master-config.yaml (SSM is now the only
#     live source — the container MUST be started via this wrapper, not step3).
#   - After km-infra rotates any /km/<env>/* SSM param.
#   - After a host reboot, once imports_container needs to come back up sourcing
#     secrets from SSM.
#
# RELATIONSHIP TO step3:
#   step3-app-stack.sh writes deployment/.env from master-config.yaml, including
#   the secret lines (ENC_KEY/ES_ADMIN_PASS/FLASK_SECRET_KEY/GITHUB_TOKEN). This
#   wrapper OVERWRITES those lines (and appends the rest) with the SSM values, in
#   place, leaving every non-secret line untouched. Re-running step3 would
#   restore the YAML values — so on an SSM-sourced host, re-run THIS wrapper
#   after any step3 run. (Phase 3 removes the secret lines from step3 per-env.)
#
# Run it as the box's deploy user (ubuntu) WITHOUT an outer sudo — mirrors
# start-imports-container.sh. The
# wrapper internally sudo-escalates only the AWS SSM fetch (SSM_SUDO=1, default).
#
# PER-ENVIRONMENT reader identity (see SSM_PROFILE / SSM_SUDO below):
#   dev/staging:  bash deployment/start-secrets-container.sh       (defaults: SSM_ENV=dev, SSM_SUDO=1, profile=bootstrap)
#   prod:     SSM_ENV=prod SSM_PROFILE="" SSM_SUDO=1 bash deployment/start-secrets-container.sh
#             (prod's [default] profile is the IP-locked SSM reader
#              ssm-reader-elastic-stack-prod and lives in ROOT's ~/.aws —
#              confirmed 2026-07-31: bare aws as ubuntu falls through to the
#              AWS-owned Lightsail role (account 088792105488 in the error =
#              the tell) and gets AccessDenied. SSM_SUDO=1 is required.)

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
REPO_DIR="${REPO_DIR:-/home/ubuntu/kavanah-analytics}"
DEPLOY_DIR="$REPO_DIR/deployment"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.yml"
COMPOSE_ENV="$DEPLOY_DIR/.env"

# Which environment's params to read. The /km tree is keyed by env:
# /km/<env>/deam/* and /km/<env>/elasticsearch/*.
#
# THERE ARE EXACTLY TWO TREES: /km/dev/ and /km/prod/. "dev" and "staging" are
# ONE environment (the staging box reads /km/dev/*) — there is no /km/staging/.
# This default was "staging" until 2026-08-03, which made a bare invocation on
# the staging box request a path that has never existed. The reader there
# (km-ssm-reader-elastic-stack-dev) is scoped to /km/dev/*, so the miss
# surfaced as an AccessDenied on /km/staging/* and read as an IAM problem
# rather than a wrong path. Prod always passes SSM_ENV=prod explicitly, so the
# broken default was only reachable on the one box nobody scripts against.
SSM_ENV="${SSM_ENV:-dev}"
SSM_REGION="${SSM_REGION:-us-west-2}"
# Host AWS profile scoped to read /km/<env>/* + kms:Decrypt. The reader identity
# differs PER ENVIRONMENT:
#   - staging: a named 'bootstrap' profile in ROOT's ~/.aws/config (the box
#     default profile is the certbot/route53 user with NO SSM read), so it must
#     be reached as `sudo aws --profile bootstrap`. This is the default here.
# - prod: the box's [default] profile IS the IP-locked SSM reader (a teammate,
#     2026-06-24), so prod uses BARE `aws` with NO --profile flag. Set
#     SSM_PROFILE="" to drop the flag.
# SSM_PROFILE="" (empty) => no --profile flag at all (use the ambient default).
AWS_PROFILE_NAME="${SSM_PROFILE-bootstrap}"
# Whether to sudo-escalate the AWS fetch. staging's 'bootstrap' lives in root's
# ~/.aws so it needs sudo (=1, default); if the reader profile resolves for the
# invoking user (e.g. prod's [default] under `ubuntu`), set SSM_SUDO=0.
SSM_SUDO="${SSM_SUDO:-1}"

SSM_PATH="/km/${SSM_ENV}/"

# ── SSM param (kebab, relative to SSM_PATH) → compose .env var name ──────────
# The env var names are what compose / the app actually read;
# they are NOT derivable from the kebab param names, so the mapping is explicit.
# A param present in SSM but absent here is fetched but skipped (logged), so a
# new SSM param can't silently leak in under an unvetted env name.
declare -A PARAM_TO_ENV=(
    ["deam/enc-key"]="ENC_KEY"
    ["deam/flask-secret-key"]="FLASK_SECRET_KEY"
    ["deam/github-read-only-token"]="GITHUB_TOKEN"
    ["deam/yubico-secret-key"]="YUBICO_SECRET_KEY"
    ["deam/anthropic-api-key"]="ANTHROPIC_API_KEY"
    ["deam/jira-api-token"]="JIRA_API_TOKEN"
    ["deam/stripe-secret-key"]="STRIPE_SECRET_KEY"
    ["deam/stripe-webhook-secret"]="STRIPE_WEBHOOK_SECRET"
    # follow-on (2026-07-30): SSO client secrets for the KM-owned
    # app registrations. Seeded on both /km/dev/ and /km/prod/; config.py
    # reads the env names first, so master-config no longer needs the values.
    ["deam/microsoft-sso-client-secret"]="MICROSOFT_SSO_CLIENT_SECRET"
    ["deam/google-sso-client-secret"]="GOOGLE_SSO_CLIENT_SECRET"
    # (2026-07-31): Google connector secrets — the shared OAuth
    # client (Ads + GA4 + YouTube) and the Ads developer token.
    # Rotation = update the SSM param, re-run this wrapper (mirrors the
    # app-backups note in start-imports-container.sh). client-secret is
    # seeded with the ROTATED value only — never the on-disk one; until it
    # exists in SSM the app loader falls back to the on-disk files, which
    # must not be deleted before staging AND prod verify env-sourced values.
    ["google-oauth-client/client-id"]="GOOGLE_OAUTH_CLIENT_ID"
    ["google-oauth-client/client-secret"]="GOOGLE_OAUTH_CLIENT_SECRET"
    ["google-ads/developer-token"]="GOOGLE_ADS_DEVELOPER_TOKEN"
    # (2026-09-26): TikTok for Business app credentials. Seeded on
    # /km/dev/ only (the sandbox app); /km/prod/ stays empty until the
    # production app passes review. Listed in CLEAR_IF_ABSENT so
    # deleting a param blanks its .env line instead of leaving an old key.
    ["tiktok/client-key"]="TIKTOK_CLIENT_KEY"
    ["tiktok/client-secret"]="TIKTOK_CLIENT_SECRET"
    # (2026-09-09): secret salt for the demo-clone pseudonyms. Seed
    # with `openssl rand -hex 32`; rotation = new param value, re-run this
    # wrapper, then clone --full --recreate (every pseudonym changes).
    ["deam/demo-clone-salt"]="DEMO_CLONE_SALT"
    # (2026-09-10): Fernet key rotation. enc-key is the PRIMARY (every
    # new ciphertext); enc-key-previous is OPTIONAL and holds the retiring
    # key(s), comma-separated, only for the rotation window. Runbook:
    # bin/python/import_scripts/ENCRYPTION.md "Key Rotation". Listed in
    # CLEAR_IF_ABSENT below so deleting the SSM param blanks the .env line.
    ["deam/enc-key-previous"]="ENC_KEY_PREVIOUS"
    ["elasticsearch/elastic-password"]="ES_ADMIN_PASS"
    # API key for the ES Security-API WRITE client (de_es_admin,
    # manage_security only). Seeded by km-infra; until then the
    # param is absent, the env var stays unset and lib/es_helpers falls back to
    # the shared client (safe by absence). Listed in CLEAR_IF_ABSENT below so
    # deleting the param later blanks the .env line (key revoke = fallback).
    ["elasticsearch/de-es-admin-api-key"]="ES_ADMIN_API_KEY"
    # Kibana gate service account (ES role de_kibana_gate: cluster
    # grant_api_key + explicit run_as list). Provisioned by km-infra
    # admin-tools/provision-kibana-gate-account.sh, which writes both params;
    # absent params = feature off (compose defaults both to empty).
    ["elasticsearch/de-kibana-gate-user"]="KIBANA_GATE_USER"
    ["elasticsearch/de-kibana-gate-password"]="KIBANA_GATE_PASSWORD"
    # kibana-system-password / kibana-token are consumed by step3-app-stack.sh
    # writing kibana.yml (deploy layer), NOT by imports_container — intentionally
    # NOT injected here. Their migration is a step3 change.
)

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
docker compose version >/dev/null 2>&1 || error "Docker Compose v2 plugin missing."

[[ -f "$COMPOSE_FILE" ]] || error "Compose file not found at $COMPOSE_FILE — run step3-app-stack.sh first."
[[ -f "$COMPOSE_ENV" ]] || error "Compose env file not found at $COMPOSE_ENV — run step3-app-stack.sh first."

# ── 1. Fetch the /km/<env>/* tree from SSM under the scoped reader profile ────
# One recursive, decrypted call pulls both deam/* and elasticsearch/* (and
# decrypts the enc-key CMK + default-key params together). Name+Value as
# tab-separated text; values are read positionally to tolerate spaces in a
# value. The reader identity (profile + whether to sudo) varies per env — see
# SSM_PROFILE / SSM_SUDO above. Built as an array so the optional --profile flag
# and optional sudo prefix are added cleanly (an empty profile = no flag).
AWS_CMD=()
[[ "$SSM_SUDO" == "1" ]] && AWS_CMD+=(sudo)
AWS_CMD+=(aws)
[[ -n "$AWS_PROFILE_NAME" ]] && AWS_CMD+=(--profile "$AWS_PROFILE_NAME")

profile_desc="${AWS_PROFILE_NAME:-<default>}"
info "Fetching SSM params under '$SSM_PATH' (profile '$profile_desc', region '$SSM_REGION', sudo=$SSM_SUDO)..."
SSM_OUT=$("${AWS_CMD[@]}" ssm get-parameters-by-path \
    --region "$SSM_REGION" \
    --path "$SSM_PATH" \
    --recursive \
    --with-decryption \
    --query "Parameters[].[Name,Value]" \
    --output text 2>&1) \
    || error "SSM fetch failed (identity '$profile_desc' may lack ssm:GetParametersByPath/kms:Decrypt on ${SSM_PATH}*): $SSM_OUT"

[[ -n "$SSM_OUT" ]] || error "SSM returned no params under ${SSM_PATH} — check SSM_ENV ('$SSM_ENV') and that Phase 0 created the tree."

# ── 2. Map params → env vars (validate fully BEFORE touching .env) ───────────
declare -A NEW_VALUES=()
mapped=0
skipped=0
while IFS=$'\t' read -r name value; do
    [[ -z "$name" ]] && continue
    rel="${name#"$SSM_PATH"}"           # /km/<env>/deam/enc-key -> deam/enc-key
    env_name="${PARAM_TO_ENV[$rel]:-}"
    if [[ -z "$env_name" ]]; then
        warn "no env-var mapping for SSM param '$name' (rel '$rel') — skipped"
        skipped=$((skipped + 1))
        continue
    fi
    NEW_VALUES["$env_name"]="$value"
    mapped=$((mapped + 1))
done <<< "$SSM_OUT"

[[ "$mapped" -gt 0 ]] || error "No SSM params mapped to env vars — refusing to rewrite $COMPOSE_ENV."

# Hard guard on the master secret: a missing/empty ENC_KEY would make every
# stored connector token undecryptable once the YAML value is stripped (Phase 3).
[[ -n "${NEW_VALUES[ENC_KEY]:-}" ]] || error "ENC_KEY not present (or empty) in the fetched SSM params — aborting (it decrypts every connector token)."

# Rotation-window vars are written BLANK when their SSM param is absent, so the
# retire step ("delete enc-key-previous, re-run this wrapper") actually removes
# the old key from .env and the container. Every other mapped var keeps the
# existing behavior (an absent param leaves an existing .env line untouched).
# ES_ADMIN_API_KEY is cleared too: revoking the de_es_admin key means
# "delete the SSM param, re-run this wrapper", and the app must drop back to the
# shared client rather than keep writing with a dead key.
# KIBANA_GATE_USER / KIBANA_GATE_PASSWORD for the same reason. The
# feature is off *by absence* only on a fresh .env; once the params have been
# seeded, deleting them (provision-kibana-gate-account.sh --revoke) would leave
# the old lines in place, so KibanaGateService.enabled() stays true and every
# mint authenticates as a service account that no longer exists. Blanking them
# is what actually turns the gate off and drops users back to Kibana's own login.
CLEAR_IF_ABSENT=(ENC_KEY_PREVIOUS ES_ADMIN_API_KEY KIBANA_GATE_USER KIBANA_GATE_PASSWORD TIKTOK_CLIENT_KEY TIKTOK_CLIENT_SECRET)
for env_name in "${CLEAR_IF_ABSENT[@]}"; do
    if [[ -z "${NEW_VALUES[$env_name]+x}" ]]; then
        NEW_VALUES["$env_name"]=""
        info "$env_name has no SSM param — writing it blank (revoked / rotation window closed)"
    fi
done

# ── 3. Rewrite the secret lines of deployment/.env in place ──────────────────
# Build a new file in a temp, replacing any existing `^ENV=` line with the SSM
# value and appending the env vars that had no line yet. Non-secret lines pass
# through untouched. Atomic move at the end so a partial write never lands.
#
# the wrapper's own header is kept to exactly one. It used to be
# appended on every run, even with nothing to append, so a host's .env grew
# one "Do not edit" block per rotation (21 stacked on staging by 2026-09-28).
# Now the first header is refreshed in place, later copies are dropped along
# with the blank line that precedes each, and a header is appended only when
# the file had none. Any other comment or blank line passes through unchanged,
# so existing hosts converge on their next run with no one-off cleanup.
info "Rewriting secret lines in $COMPOSE_ENV ($mapped mapped, $skipped skipped)..."
TMP_ENV="$(mktemp "${COMPOSE_ENV}.XXXXXX")"
# shellcheck disable=SC2064  # expand TMP_ENV now so the trap cleans the right file
trap "rm -f '$TMP_ENV'" EXIT

HEADER_LINE_1_PREFIX="# Phase 2 — config secrets fetched from AWS SSM "
HEADER_LINE_2_PREFIX="# by start-secrets-container.sh on "
write_header() {
    echo "${HEADER_LINE_1_PREFIX}${SSM_PATH}*"
    echo "${HEADER_LINE_2_PREFIX}$(date -u +'%Y-%m-%dT%H:%M:%SZ'). Do not edit."
}

declare -A WRITTEN=()
header_written=0
pending_blank=0   # a blank line is held back until we know it does not precede a duplicate header
while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ -z "$line" ]]; then
        [[ "$pending_blank" == 1 ]] && printf '\n' >> "$TMP_ENV"
        pending_blank=1
        continue
    fi
    if [[ "$line" == "$HEADER_LINE_1_PREFIX"* ]]; then
        if [[ "$header_written" == 0 ]]; then
            [[ "$pending_blank" == 1 ]] && printf '\n' >> "$TMP_ENV"
            write_header >> "$TMP_ENV"
            header_written=1
        fi
        pending_blank=0
        continue
    fi
    if [[ "$line" == "$HEADER_LINE_2_PREFIX"* ]]; then
        pending_blank=0
        continue
    fi
    [[ "$pending_blank" == 1 ]] && printf '\n' >> "$TMP_ENV"
    pending_blank=0
    key="${line%%=*}"
    if [[ "$line" == *"="* && -n "${NEW_VALUES[$key]+x}" ]]; then
        printf '%s=%s\n' "$key" "${NEW_VALUES[$key]}" >> "$TMP_ENV"
        WRITTEN["$key"]=1
    else
        printf '%s\n' "$line" >> "$TMP_ENV"
    fi
done < "$COMPOSE_ENV"
[[ "$pending_blank" == 1 ]] && printf '\n' >> "$TMP_ENV"

# Append any mapped vars that didn't already have a line (the 5 secrets
# step3 doesn't write: Yubico/Anthropic/Jira/Stripe x2).
appended=0
if [[ "$header_written" == 0 ]]; then
    { echo ""; write_header; } >> "$TMP_ENV"
fi
for env_name in "${!NEW_VALUES[@]}"; do
    if [[ -z "${WRITTEN[$env_name]:-}" ]]; then
        printf '%s=%s\n' "$env_name" "${NEW_VALUES[$env_name]}" >> "$TMP_ENV"
        appended=$((appended + 1))
    fi
done

chmod 600 "$TMP_ENV"
mv "$TMP_ENV" "$COMPOSE_ENV"

# restamp the owner after the atomic move.
# `mv` swaps the INODE: $COMPOSE_ENV is now the mktemp file, owned by whoever
# invoked this wrapper, not by whoever owned the .env we just replaced. The
# header above says to run this WITHOUT an outer sudo, but a runbook
# documents a sudo invocation and staging's .env was root:root 600 as of
# 2026-09-23 — so it happens. `ubuntu` then cannot read the file: compose and
# every later wrapper run need sudo too, and the first one that forgets fails
# on an unreadable env file. That is the blast radius.
# Deliberately NOT a root guard here (step3 has one): a guard would turn a sudo
# run into a hard failure mid-rotation, and the documented path would break.
# Stamping the owner makes such a run harmless instead, which is the fix.
# The owner is read from $REPO_DIR — never $USER/$SUDO_USER, which under sudo
# depend on env_reset/env_keep, while the checkout's owner is the deploy user by
# construction. Non-fatal: a failed chown must not abort a secrets rotation.
ENV_OWNER="$(stat -c '%U:%G' "$REPO_DIR" 2>/dev/null || stat -f '%Su:%Sg' "$REPO_DIR" 2>/dev/null || true)"
if [[ -n "$ENV_OWNER" ]]; then
    chown "$ENV_OWNER" "$COMPOSE_ENV" 2>/dev/null \
        || warn "could not chown $COMPOSE_ENV to $ENV_OWNER — confirm the deploy user can still read it"
else
    ENV_OWNER="<unknown>"
    warn "could not read the owner of $REPO_DIR — leaving $COMPOSE_ENV owned by the invoking user"
fi

trap - EXIT
success "$COMPOSE_ENV updated (${#WRITTEN[@]} replaced, $appended appended; mode 600, owner $ENV_OWNER)"

# ── 4a. Rebuild the image before recreating ─────────────────────────
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

# ── 4. Recreate imports_container via compose ────────────────────────────────
# --force-recreate so the rewritten.env is actually picked up.
# SIGTERM trap drains the old container cleanly within Docker's grace period.
info "Recreating imports_container via docker compose..."
docker compose -f "$COMPOSE_FILE" --env-file "$COMPOSE_ENV" \
    up -d --force-recreate imports_container

success "imports_container recreated with secrets injected from SSM (${SSM_PATH}*)."
echo ""
echo "Verify:"
echo "  docker exec imports_container printenv ENC_KEY     # should be non-empty"
echo "  docker compose -f $COMPOSE_FILE ps imports_container   # running + healthy"
